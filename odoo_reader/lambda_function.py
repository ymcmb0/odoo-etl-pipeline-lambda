import os
import json
import requests
import boto3
from datetime import datetime

# ===== Environment Variables =====
ODOO_URL = os.getenv("ODOO_URL", "").strip()
ODOO_DB = os.getenv("ODOO_DB", "").strip()
ODOO_USER = os.getenv("ODOO_USER", "").strip()
ODOO_PASS = os.getenv("ODOO_PASS", "").strip()

AWS_REGION = os.getenv("AWS_REGION", "eu-north-1")
S3_BUCKET = os.getenv("S3_BUCKET", "odoo-timesheet-bucket")

# ===== Initialize S3 Client =====
# The Lambda's Execution Role MUST have s3:GetObject and s3:PutObject permissions
s3_client = boto3.client("s3", region_name=AWS_REGION)

# Local in-memory store of write_date timestamps (Not persisted across runs)
last_load_times = {}

# ==============================================================================
# Odoo and Data Helpers (Authentication, Read, Normalize)
# ==============================================================================

def odoo_authenticate():
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": "common",
            "method": "authenticate",
            "args": [ODOO_DB, ODOO_USER, ODOO_PASS, {}],
        },
        "id": 1,
    }
    try:
        # Use a short timeout for authentication
        res = requests.post(f"{ODOO_URL}/jsonrpc", json=payload, timeout=10).json()
        if "error" in res:
            print(f"Odoo Auth Error: {res['error']}")
            return None
        return res.get("result")
    except requests.exceptions.RequestException as e:
        print(f"HTTP Request Error during Odoo Auth: {e}")
        return None

def odoo_search_read(model, fields, domain=None, limit=0):
    uid = odoo_authenticate()
    if not uid:
        print(f"Skipping search for {model} due to auth failure.")
        return []
    
    domain = domain or []
    params = {
        "service": "object",
        "method": "execute_kw",
        "args": [
            ODOO_DB,
            uid,
            ODOO_PASS,
            model,
            "search_read",
            [domain],
            {"fields": fields, "limit": limit} if limit else {"fields": fields}
        ]
    }
    payload = {"jsonrpc": "2.0", "method": "call", "params": params, "id": 2}
    try:
        # Use a longer timeout for reading data
        res = requests.post(f"{ODOO_URL}/jsonrpc", json=payload, timeout=30)
        res.raise_for_status() # Raise exception for bad status codes (4xx or 5xx)
        return res.json().get("result", [])
    except requests.exceptions.RequestException as e:
        print(f"HTTP Request Error during Odoo search_read for {model}: {e}")
        return []

def normalize_odoo_row(row: dict) -> dict:
    new_row = {}
    for k, v in row.items():
        if isinstance(v, list):
            if len(v) == 2 and isinstance(v[0], int) and isinstance(v[1], str):
                new_row[f"{k}_id"] = v[0]
                new_row[f"{k}_name"] = v[1]
            elif all(isinstance(x, int) for x in v):
                new_row[k] = ",".join(map(str, v))
            else:
                new_row[k] = json.dumps(v)
        elif v is False or v is None:
            new_row[k] = ""
        else:
            new_row[k] = v
    return new_row

def normalize_odoo_data(data: list) -> list:
    return [normalize_odoo_row(r) for r in data]

# ==============================================================================
# S3 Upload and URL Generation (MODIFIED)
# ==============================================================================

def push_to_s3(table_name, data):
    if not data:
        # If no data is found, we still return a response but skip the upload
        return {"status": "empty", "message": "No data to upload"}

    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    latest_key = f"{table_name}/latest.json"
    timestamped_key = f"{table_name}/{table_name}_{timestamp}.json"
    
    # 1. Upload timestamped file
    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=timestamped_key,
        Body=json.dumps(data),
        ContentType="application/json"
    )
    # 2. Upload 'latest.json' file
    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=latest_key,
        Body=json.dumps(data),
        ContentType="application/json"
    )
    
    # 3. Generate a Pre-Signed URL for the 'latest.json' file
    try:
        # This URL is valid for 5 minutes (300 seconds)
        presigned_url = s3_client.generate_presigned_url(
            ClientMethod='get_object',
            Params={'Bucket': S3_BUCKET, 'Key': latest_key},
            ExpiresIn=300 
        )
    except Exception as e:
        print(f"ERROR generating pre-signed URL: {e}")
        return {"status": "error", "message": f"Could not generate S3 URL: {e}"}

    # 4. Return the secure URL for Power BI
    return {
        "status": "success", 
        "s3_key": latest_key, # Keep for debugging
        "url": presigned_url, # <-- The field Power BI needs
        "rows_uploaded": len(data)
    }


# ==============================================================================
# Lambda Handler (UPDATED FOR SINGLE-TABLE EXECUTION)
# ==============================================================================

def lambda_handler(event, context):
    print("EVENT RECEIVED:", json.dumps(event))

    query_params = event.get("queryStringParameters", {})
    requested_table = query_params.get("table")
    
    # --- MODEL & FIELD DEFINITIONS ---
    all_tables = [
        "projects", "sale_orders", "invoices", "partners",
        "users", "timesheets", "project_updates"
    ]
    table_model_map = {
        "projects": "project.project", "sale_orders": "sale.order", "partners": "res.partner", 
        "users": "res.users", "invoices": "account.move", "project_updates": "project.update", 
        "timesheets": "account.analytic.line",
    }
    table_fields = {
        "projects": ["id", "display_name", "allocated_hours", "date_start", "date", "description", "partner_id", "sale_order_id", "update_ids", "user_id", "tag_ids", "stage_id", "write_date"],
        "sale_orders": ["id", "name", "display_name", "partner_id", "amount_total", "amount_untaxed", "amount_unpaid", "amount_paid", "amount_invoiced", "amount_to_invoice", "margin", "approva_state", "state", "date_order", "pricelist_id", "opportunity_id", "payment_term_id", "project_ids", "invoice_ids", "user_id", "write_date"],
        "partners": ["id", "name", "write_date"],
        "users": ["id", "name", "write_date"],
        "timesheets": ["id", "name", "employee_id", "user_id", "department_id", "project_id", "validated_status", "date", "unit_amount", "task_id", "timesheet_invoice_type", "write_date"],
        "project_updates": ["id", "name", "project_id", "user_id", "date", "write_date", "progress", "description"],
        "invoices": ["id", "name", "partner_id", "currency_id", "amount_total", "invoice_date", "payment_state", "invoice_date_due", "invoice_payment_term_id", "invoice_line_ids", "state", "write_date"],
    }

    final_result = {}

    if requested_table and requested_table in all_tables:
        table = requested_table
        model = table_model_map.get(table)
        fields = table_fields.get(table)
        
        print(f"Starting ETL for table: {table}")
        
        if not model or not fields:
            final_result = {"status": "error", "message": f"Table {table} configuration error"}
        else:
            # 1. Extract (Read from Odoo)
            data = odoo_search_read(model, fields)
            
            # 2. Transform (Normalize data)
            normalized = normalize_odoo_data(data)

            # 3. Load (Push to S3 and generate URL)
            s3_upload_result = push_to_s3(table, normalized)
            
            # 4. Return the result (which contains the 'url' field)
            final_result = s3_upload_result
            
    else:
        # If no table is supplied or is invalid, return an error that Power BI can process
        final_result = {"status": "error", "message": f"Missing or invalid 'table' parameter: {requested_table}"}
        
    # API Gateway expects a dictionary response
    return final_result
    
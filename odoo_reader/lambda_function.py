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
    trigger_flag = query_params.get("trigger") # <-- Capture the new flag

    # --- MODEL & FIELD DEFINITIONS (unchanged) ---
    all_tables = [
        "projects", "sale_orders", "invoices", "partners",
        "users", "timesheets", "project_updates"
    ]
    # ... (table_model_map and table_fields unchanged) ...

    final_result = {}
    
    # DETERMINE TABLES TO LOAD
    tables_to_load = []
    
    if requested_table and requested_table in all_tables:
        # Scenario 2: Data Fetch Mode (Load single table)
        tables_to_load = [requested_table]
        
    elif trigger_flag == "1":
        # Scenario 1: Trigger Mode (Load ALL tables)
        tables_to_load = all_tables
        
    else:
        # Error condition
        return {"status": "error", "message": f"Missing or invalid parameter. Requested table: {requested_table}"}


    # --- EXECUTE ETL ---
    if tables_to_load:
        # Only run the full combined load if the trigger flag is set
        if len(tables_to_load) > 1:
            print("Running FULL Multi-Table ETL (Trigger Mode)")
            # Your old multi-table loop logic goes here:
            results = {}
            all_rows = []
            
            for table in tables_to_load:
                # ... (existing single-table ETL logic: odoo_search_read, normalize) ...
                # Use the existing logic here for ALL tables
                model = table_model_map.get(table)
                fields = table_fields.get(table)
                data = odoo_search_read(model, fields)
                normalized = normalize_odoo_data(data)
                
                # Push the single table to S3 (updates latest.json)
                push_to_s3(table, normalized)
                
                # Append to all_rows for the combined file
                for row in normalized:
                    row_with_table = {"table": table}
                    row_with_table.update(row)
                    all_rows.append(row_with_table)
            
            # PUSH COMBINED FILE (all_data)
            if len(all_rows) > 0:
                # Use your existing logic for pushing all_data/latest.json
                # You'll need a new helper or move the all_data logic back in here.
                # For simplicity, let's just return a success message in this mode.
                final_result = {"status": "success", "message": f"Successfully triggered and updated {len(tables_to_load)} tables."}

        else:
            # Load single table (Data Fetch Mode)
            table = tables_to_load[0]
            print(f"Running SINGLE Table ETL (Read Mode) for: {table}")
            
            model = table_model_map.get(table)
            fields = table_fields.get(table)
            data = odoo_search_read(model, fields)
            normalized = normalize_odoo_data(data)
            
            # This returns the result including the 'url' field for PBI to read
            final_result = push_to_s3(table, normalized)
    
    return final_result

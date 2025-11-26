import os
import json
import requests
import boto3
from datetime import datetime
from urllib.parse import parse_qs

# ===== Environment Variables =====
ODOO_URL = os.getenv("ODOO_URL", "").strip()
ODOO_DB = os.getenv("ODOO_DB", "").strip()
ODOO_USER = os.getenv("ODOO_USER", "").strip()
ODOO_PASS = os.getenv("ODOO_PASS", "").strip()

AWS_REGION = os.getenv("AWS_REGION", "eu-north-1")
S3_BUCKET = os.getenv("S3_BUCKET", "odoo-timesheet-bucket")

# ===== Initialize S3 Client =====
s3_client = boto3.client("s3", region_name=AWS_REGION)

# Local in-memory store of write_date timestamps
last_load_times = {}

# ===== Odoo Helpers =====
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
    res = requests.post(f"{ODOO_URL}/jsonrpc", json=payload).json()
    return res.get("result")

def odoo_search_read(model, fields, domain=None, limit=0):
    uid = odoo_authenticate()
    if not uid:
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
    res = requests.post(f"{ODOO_URL}/jsonrpc", json=payload)
    return res.json().get("result", [])

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

# ===== S3 Upload =====
def push_to_s3(table_name, data):
    if not data:
        return {"status": "empty", "message": "No data to upload"}

    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    key = f"{table_name}/{table_name}_{timestamp}.json"

    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=json.dumps(data),
        ContentType="application/json"
    )
    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=f"{table_name}/latest.json",
        Body=json.dumps(data),
        ContentType="application/json"
    )
    return {"status": "success", "s3_key": key, "rows_uploaded": len(data)}

# ===== API Error Helper =====
def error(message):
    return {
        "statusCode": 400,
        "body": json.dumps({"error": message}),
        "headers": {"Content-Type": "application/json"}
    }

# ===== Lambda Handler =====
def lambda_handler(event, context):
    print("EVENT RECEIVED:", json.dumps(event))

    # ==========================
    # Read Query Parameters
    # ==========================
    table = None
    update_request = None

    if "queryStringParameters" in event:
        params = event.get("queryStringParameters") or {}
        table = params.get("table")
        update_request = params.get("update")
    elif "rawQueryString" in event:
        raw_qs = event.get("rawQueryString", "")
        params = {k: v[0] for k, v in parse_qs(raw_qs).items()}
        table = params.get("table")
        update_request = params.get("update")

    # ==========================
    # CASE 1: Return Presigned URL
    # ==========================
    if table:
        key = f"{table}/latest.json"
        try:
            url = s3_client.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": S3_BUCKET, "Key": key},
                ExpiresIn=900
            )
        except Exception as e:
            return error(f"Failed to generate presigned URL: {str(e)}")

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"url": url})
        }

    # ==========================
    # CASE 2: update=true -> refresh ALL tables
    # ==========================
    if update_request == "true":
        event["tables"] = [
            "projects", "sale_orders", "invoices", "partners",
            "users", "timesheets", "project_updates"
        ]

    # ==========================
    # Parse body if exists
    # ==========================
    if "body" in event and event["body"]:
        try:
            body = json.loads(event["body"])
            if isinstance(body, dict):
                event.update(body)
        except Exception as e:
            print("Failed to parse body:", str(e))

    # ==========================
    # Determine tables to load
    # ==========================
    if "table" in event:
        tables = [event["table"]]
    elif "tables" in event:
        tables = event["tables"]
    elif event.get("all"):
        tables = [
            "projects", "sale_orders", "invoices", "partners",
            "users", "timesheets", "project_updates"
        ]
    else:
        return {"status": "error", "message": "No table(s) specified"}

    # ==========================
    # MODEL & FIELD DEFINITIONS
    # ==========================
    table_model_map = {
        "projects": "project.project",
        "sale_orders": "sale.order",
        "partners": "res.partner",
        "users": "res.users",
        "invoices": "account.move",
        "project_updates": "project.update",
        "timesheets": "account.analytic.line",
    }

    table_fields = {
        "projects": ["id", "display_name", "allocated_hours","date_start","date","description",
                     "partner_id","sale_order_id","update_ids","user_id","tag_ids","stage_id","write_date"],
        "sale_orders": ["id", "name","display_name", "partner_id", "amount_total",
                        "amount_untaxed","amount_unpaid","amount_paid","amount_invoiced",
                        "amount_to_invoice","margin","approva_state","state","date_order",
                        "pricelist_id","opportunity_id","payment_term_id","project_ids","invoice_ids","user_id","write_date"],
        "partners": ["id","name","write_date"],
        "users": ["id","name","write_date"],
        "timesheets": [
            "id","name","employee_id","user_id","department_id","project_id","validated_status","date",
            "unit_amount","task_id","timesheet_invoice_type",
           "write_date"],
        "project_updates": ["id","name","project_id","user_id","date","write_date","progress","description"],
        "invoices": ["id","name","partner_id","currency_id","amount_total","invoice_date","payment_state",
                     "invoice_date_due","invoice_date","invoice_payment_term_id","invoice_line_ids","state","write_date"],
    }

    results = {}
    all_rows = []

    # ==========================
    # LOAD EACH TABLE + PUSH TO S3
    # ==========================
    for table in tables:
        model = table_model_map.get(table)
        fields = table_fields.get(table)
        if not model or not fields:
            results[table] = {"status": "error", "message": f"Table {table} not configured"}
            continue

        data = odoo_search_read(model, fields)
        normalized = normalize_odoo_data(data)

        if normalized:
            last_load_times[table] = max(
                r["write_date"] for r in normalized if "write_date" in r
            )

        results[table] = push_to_s3(table, normalized)

        for row in normalized:
            row_with_table = {"table": table}
            row_with_table.update(row)
            all_rows.append(row_with_table)

    # ==========================
    # Combined File for ALL DATA
    # ==========================
    if len(all_rows) > 0 and (len(tables) > 1 or event.get("all")):
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        combined_key = f"all_data/all_data_{timestamp}.json"

        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=combined_key,
            Body=json.dumps(all_rows),
            ContentType="application/json"
        )
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key="all_data/latest.json",
            Body=json.dumps(all_rows),
            ContentType="application/json"
        )
        results["all_data"] = {"status": "success", "s3_key": combined_key, "rows_uploaded": len(all_rows)}

    return results

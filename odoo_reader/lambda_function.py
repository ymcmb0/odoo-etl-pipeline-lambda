import boto3
import os
import json
from urllib.parse import parse_qs

# S3 client
s3 = boto3.client("s3")
BUCKET = os.getenv("S3_BUCKET", "odoo-timesheet-bucket")

# Allowed tables
VALID_TABLES = [
    "projects", "sale_orders", "partners", "users",
    "timesheets", "pmo", "invoices"
]

def lambda_handler(event, context):
    print("EVENT RECEIVED:", event)

    # ---- READ QUERY PARAMETERS SAFELY ----
    params = event.get("queryStringParameters")
    if params is None:
        raw_qs = event.get("rawQueryString", "")
        params = {k: v[0] for k, v in parse_qs(raw_qs).items()}

    table = params.get("table")
    if not table:
        return error("Missing 'table' parameter, e.g. ?table=timesheets")

    if table not in VALID_TABLES:
        return error(f"Invalid table name: {table}. Allowed: {', '.join(VALID_TABLES)}")

    key = f"{table}/latest.json"

    # ---- GENERATE PRESIGNED URL ----
    try:
        url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": BUCKET, "Key": key},
            ExpiresIn=900  # 15 minutes
        )
    except Exception as e:
        return error(f"Failed to generate presigned URL: {str(e)}")

    # ---- RETURN JSON WITH URL ----
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"url": url})
    }

def error(message):
    return {
        "statusCode": 400,
        "body": json.dumps({"error": message}),
        "headers": {"Content-Type": "application/json"}
    }

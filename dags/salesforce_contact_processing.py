from airflow import DAG
from airflow.decorators import task

from datetime import datetime, timedelta
from airflow.providers.postgres.operators.postgres import PostgresOperator
from airflow.providers.http.sensors.http import HttpSensor
from airflow.operators.python import PythonOperator

from airflow.providers.postgres.hooks.postgres import PostgresHook

import os
import requests
from pandas import json_normalize, notna  # used in _process_contact

SALESFORCE_CONSUMER_KEY = os.getenv("SALESFORCE_CONSUMER_KEY")
SALESFORCE_CONSUMER_SECRET = os.getenv("SALESFORCE_CONSUMER_SECRET")
SALESFORCE_USER_NAME = os.getenv("SALESFORCE_USER_NAME")
SALESFORCE_PASSWORD = os.getenv("SALESFORCE_PASSWORD")
SALESFORCE_SECURITY_TOKEN = os.getenv("SALESFORCE_SECURITY_TOKEN")
SALESFORCE_INSTANCE_URL = os.getenv("SALESFORCE_INSTANCE_URL")


def _get_department(row):
    if notna(row["Department"]) or row["Title"] is None:
        return row["Department"]

    else:
        return row["Title"].split(",")[1].strip()


def _get_one_minute_ago_time():
    # Get the current time
    current_time = datetime.utcnow()

    # Calculate 1 minute ago
    one_minute_ago = current_time - timedelta(
        minutes=1,
        seconds=current_time.second,
        microseconds=current_time.microsecond,
    )

    # Format the result in ISO 8601 format
    iso_format = one_minute_ago.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return iso_format


def _generate_token():
    payload = {
        "grant_type": "password",
        "client_id": SALESFORCE_CONSUMER_KEY,
        "client_secret": SALESFORCE_CONSUMER_SECRET,
        "username": SALESFORCE_USER_NAME,
        "password": SALESFORCE_PASSWORD + SALESFORCE_SECURITY_TOKEN,
    }
    oauth_endpoint = "/services/oauth2/token"
    response = requests.post(SALESFORCE_INSTANCE_URL + oauth_endpoint, data=payload)
    return response.json()


def _extract_contact():
    ## Check if this is inital sync or subsequential sync
    postgres_hook = PostgresHook(postgres_conn_id="postgres")

    data = postgres_hook.get_pandas_df(sql="SELECT * FROM contacts;")

    request_url = f"{SALESFORCE_INSTANCE_URL}/services/data/v59.0/query?q=SELECT+FIELDS(STANDARD)+from+Contact"

    if data.size > 0:
        request_url = (
            f"{request_url}+WHERE+LastModifiedDate+>+{_get_one_minute_ago_time()}"
        )

    access_token = _generate_token()["access_token"]

    ### Prepare the headers ###
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }

    ### Perform the request ###
    try:
        r = requests.get(
            f"{SALESFORCE_INSTANCE_URL}/services/data/v59.0/query?q=SELECT+FIELDS(STANDARD)+from+Contact",
            headers=headers,
        )
    except:
        raise Exception(f"The request went wrong")

    if r.status_code != 200:
        raise Exception(f"Something in the request went wrong: {r.status_code}")

    # Grab the data
    data = r.json()
    contact = data["records"]

    # Normalize response
    normalized_contact = json_normalize(contact)

    normalized_contact["name"] = normalized_contact.apply(
        lambda row: f'{row["Salutation"]} {row["FirstName"]} {row["LastName"]}',
        axis=1,
    )

    normalized_contact["gender"] = normalized_contact.apply(
        lambda row: "Male" if "Mr" in row["Salutation"] else "Female",
        axis=1,
    )

    normalized_contact["department"] = normalized_contact.apply(
        _get_department,
        axis=1,
    )

    result = normalized_contact[["name", "Title", "department", "Email", "gender"]]

    result = result.rename(
        columns={
            "Title": "title",
            "Email": "email",
        }
    )

    postgres_hook = PostgresHook(postgres_conn_id="postgres")

    # Save to DB
    result.to_sql(
        "contacts",
        postgres_hook.get_sqlalchemy_engine(),
        if_exists="replace",
        chunksize=1000,
        index=False,
    )


with DAG(
    "salesforce_contact_processing",
    start_date=datetime(2022, 1, 1),
    schedule_interval="* * * * *",
    catchup=False,
) as dag:
    create_table = PostgresOperator(
        task_id="create_table",
        postgres_conn_id="postgres",
        sql="""
            CREATE TABLE IF NOT EXISTS contacts (
                name TEXT NOT NULL,
                title TEXT NOT NULL,
                department TEXT NOT NULL,
                email TEXT NOT NULL,
                gender TEXT NOT NULL
            );
        """,
    )

    is_api_available = HttpSensor(
        task_id="is_api_available", http_conn_id="salesforce", endpoint=""
    )

    extract_contact = PythonOperator(
        task_id="extract_contact", python_callable=_extract_contact
    )

    create_table >> is_api_available >> extract_contact

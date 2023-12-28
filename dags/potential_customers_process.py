from numpy import NaN
from airflow import DAG
from datetime import datetime
from airflow.providers.postgres.operators.postgres import PostgresOperator
from airflow.operators.python import PythonOperator

from airflow.providers.postgres.hooks.postgres import PostgresHook

import country_converter as coco


from pandas import (
    read_excel,
    Timestamp,
    to_datetime,
)  # used in _process_user

cc = coco.CountryConverter()


def _extract_and_filter_customer():
    data = read_excel(
        "./dags/data/customer.xlsx",
        dtype={
            "Age": "Int64",
            "Id": "Int64",
        },
    )

    # Remove the first unnecessary column in the excel file
    del data[data.columns[0]]

    non_gender_condition = data["Gender"].notna()
    non_country_condition = data["Country"].notna()
    non_age_condition = data["Age"].notna()

    # Condition: we want to target adult customers only
    aldult_condition = data["Age"] >= 18

    # Condition: the marketing campaign target female only
    female_condition = data["Gender"].str.lower() == "female"

    filtered_customer = data[
        non_gender_condition
        & non_country_condition
        & non_age_condition
        & aldult_condition
        & female_condition
    ]

    postgres_hook = PostgresHook(postgres_conn_id="postgres")

    # Save to DB
    filtered_customer.to_sql(
        "filtered_customers",
        postgres_hook.get_sqlalchemy_engine(),
        if_exists="replace",
        chunksize=1000,
    )


def _transform_filtered_customers(ti, **kwargs):
    postgres_hook = PostgresHook(postgres_conn_id="postgres")

    data = postgres_hook.get_pandas_df(sql="SELECT * FROM filtered_customers;")

    ## Step 1: convert Last login to unix timestamp
    data["last_login"] = to_datetime(data["Last login"]).map(Timestamp.timestamp)

    ## Step 2: Get fullname
    data["fullname"] = data.apply(
        lambda col: "%s %s" % (col["First Name"], col["Last Name"]), axis=1
    )

    ## Step 3: Country code retrieval
    data["country_code"] = data.apply(lambda col: cc.convert(col["Country"]), axis=1)

    ## Step 4: Mapped correct column name and order
    new_data = data[["fullname", "country_code", "Gender", "Age", "last_login"]]
    new_data = new_data.rename(
        columns={
            "Gender": "gender",
            "Age": "age",
        }
    )

    # Save to DB
    new_data.to_sql(
        "customers",
        postgres_hook.get_sqlalchemy_engine(),
        if_exists="append",
        chunksize=1000,
        index=False,
    )


with DAG(
    "potential_customers_process",
    start_date=datetime(2022, 1, 1),
    schedule_interval="@daily",
    catchup=False,
) as dag:
    create_customers_table = PostgresOperator(
        task_id="create_customers_table",
        postgres_conn_id="postgres",
        sql="""
            CREATE TABLE IF NOT EXISTS customers (
                fullname TEXT NOT NULL,
                country_code TEXT NOT NULL,
                gender TEXT NOT NULL,
                age INT NOT NULL,
                last_login INT NOT NULL
            );
        """,
    )

    create_filtered_customers_table = PostgresOperator(
        task_id="create_filtered_customers_table",
        postgres_conn_id="postgres",
        sql="""
            CREATE TABLE IF NOT EXISTS filtered_customers (
                firstname TEXT NOT NULL,
                lastname TEXT NOT NULL,
                country TEXT NOT NULL,
                gender TEXT NOT NULL,
                age INT NOT NULL,
                last_login TEXT NOT NULL,
                external_id TEXT NOT NULL
            );
        """,
    )

    extract_customer = PythonOperator(
        task_id="extract_and_filter_customer",
        python_callable=_extract_and_filter_customer,
    )

    transform_filtered_customers = PythonOperator(
        task_id="transform_filtered_customers",
        python_callable=_transform_filtered_customers,
    )

    (
        create_customers_table
        >> create_filtered_customers_table
        >> extract_customer
        >> transform_filtered_customers
    )

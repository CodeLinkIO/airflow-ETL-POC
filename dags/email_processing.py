from airflow import DAG
from airflow.decorators import task
from airflow.providers.mongo.hooks.mongo import MongoHook
from hooks.imap.imap_hook import ImapHook
from pymongo import errors

from datetime import datetime


with DAG(
    dag_id="email_processing",
    schedule="* * * * *",
    start_date=datetime(2023, 1, 1),
    catchup=False,
):

    @task
    def read_dataset():
        email = ""
        message_id = ""
        mongoHook = MongoHook(mongo_conn_id="mongo_default")
        client = mongoHook.get_conn()
        db = client.MyDB
        count = 0
        email_collection = db.email_collection
        with ImapHook() as hook:
            mail_ids = hook.list_mail_ids_desc()
            for mail_id in mail_ids:
                print(f"Fetching mail {mail_id}")
                email, message_id = hook.fetch_mail_body(mail_id)
                try:
                    email_collection.insert_one({"email": email, "_id": message_id})
                    count = count + 1
                except errors.DuplicateKeyError:
                    print(f"Email {message_id} is already sync")
                    break
            hook.close()
        print(f"Finish syncing {count} email(s)")

    read_dataset()

import os
from datetime import datetime

from google.cloud import secretmanager
from flask import Flask, abort

from models.config import Config as ConfigModule
from models.config.Config import Config
from services.DataRecordFlatteningService import DataRecordFlatteningService
from services.PersonMarshMallowService import PersonMarshMallowService
from services.PlaceMarshMallowService import PlaceMarshMallowService
from services.TaskMarshMallowService import TaskMarshMallowService
from services.cloud_sql.CloudSql import connect_to_cloud_server
from models.BatchInfo import BatchInfo
from models.couchdb_doc.CouchdbDoc import CouchdbDoc
from models.couchdb_request.CouchdbAuth import CouchdbAuth
from models.couchdb_request.CouchdbRequest import CouchdbRequest
from services.CloudSqlConfigService import CloudSqlConfigService
from services.CloudSqlStatService import CloudSqlStatService
from services.CouchDBPythonService import CouchDBPythonService
from services.DataRecordMarshMallowService import DataRecordMarshMallowService
from services.GooglePubSubService import GooglePubSubService

app = Flask(__name__)


@app.route("/")
def start_loop():
    client = secretmanager.SecretManagerServiceClient()
    db = connect_to_cloud_server(client)
    if db is None:
        print('error no access to Cloud Sql db')
        abort(500)
    config_service = CloudSqlConfigService(db)
    if not config_service.init():
        print('Error: Unable to get config from cloud SQL')
        abort(500)

    stat_service = CloudSqlStatService(db)

    couchdb_changes_service = CouchDBPythonService()
    pub_sub_service = GooglePubSubService()
    data_record_schema_service = DataRecordMarshMallowService()
    flattening_service = DataRecordFlatteningService()
    person_schema_service = PersonMarshMallowService()
    place_schema_service = PlaceMarshMallowService()
    task_schema_service = TaskMarshMallowService()

    couchdb_request = CouchdbRequest()

    couchdb_auth = CouchdbAuth(client)

    print(ConfigModule.config_to_string())

    batch_id = 1
    data = couchdb_changes_service.get_batch()
    batch_info = BatchInfo(
        batch_id=batch_id,
        start_at=datetime.now(),
        start_seq=couchdb_request.since,
        pending=data.pending
    )
    for result in data.docs:
        batch_info.received = batch_info.received + 1
        if "doc" in result:
            doc = result["doc"]
            if "type" in doc:
                if doc["type"] == 'data_record':
                    batch_info.received_forms = batch_info.received_forms + 1
                    data_record = CouchdbDoc(data_record_schema_service, pub_sub_service, flattening_service)
                    if data_record.is_valid_doc(doc):
                        batch_info.validated_forms = batch_info.validated_forms + 1
                        pub_sub_topic = pub_sub_service.get_topic_from_data(doc)
                        if pub_sub_topic != '':
                            form_to_send = doc
                            if Config.flattening:
                                flat_data_record = data_record.flatten(doc)
                                if flat_data_record is not None and flat_data_record != {}:
                                    batch_info.flatten_forms = batch_info.flatten_forms + 1
                                    form_to_send = flat_data_record
                            message_id = data_record.dispatch_to_pub_sub(pub_sub_topic, form_to_send)
                    else:
                        batch_info.malformed_forms = batch_info.malformed_forms + 1
                if doc["type"] == 'person':
                    person = CouchdbDoc(person_schema_service, pub_sub_service)
                    if person.is_valid_doc(doc):
                        batch_info.validated_persons = batch_info.validated_persons + 1
                        person.dispatch_to_pub_sub('mali-prod-persons', doc)
                if "contact_type" in doc and doc["type"] == "contact":
                    place = CouchdbDoc(place_schema_service, pub_sub_service)
                    if place.is_valid_doc(doc):
                        batch_info.validated_places = batch_info.validated_places + 1
                        place.dispatch_to_pub_sub('mali-prod-places', doc)
                if doc["type"] == 'task':
                    task = CouchdbDoc(task_schema_service, pub_sub_service)
                    if task.is_valid_doc(doc):
                        batch_info.validated_tasks = batch_info.validated_tasks + 1
                        task_pub_sub_topic = pub_sub_service.get_task_topic(doc)
                        if task_pub_sub_topic != '':
                            task.dispatch_to_pub_sub('task_pub_sub_topic', doc)

    batch_info.end_seq = data.last_sequence_number
    batch_info.end_at = datetime.now()
    if not stat_service.verify_connexion():
        stat_service.db = connect_to_cloud_server(client)
        config_service.db = stat_service.db
    print("publishing batch stats")
    stat_service.publish_batch_stat(batch_info)
    print("batch stats published :=)")
    Config.last_couchdb_sequence = data.last_sequence_number
    print(f"publishing last seq {data.last_sequence_number}")
    config_service.store_sequence()
    print("last seq published :=)")

    return 'ok'


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 4080)))

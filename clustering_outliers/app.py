import json
from enum import auto, Enum
from datetime import datetime, timezone
from os import path, getenv, stat
from apispec import APISpec
from apispec_webframeworks.flask import FlaskPlugin
from flask_cors import CORS
from flask_executor import Executor
from flask_wtf import FlaskForm
from flask import Flask, send_file, abort
from flask import make_response

from . import db
from .forms import KMeansFileForm, KMeansPathForm, DBScanFileForm, DBScanPathForm, AgglomerativeFileForm, \
    AgglomerativePathForm, IsoForestFileForm, IsoForestPathForm, LOFFileForm, LOFPathForm, OCSVMPathForm, OCSVMFileForm
from .models.agglomerative_clustering import agglomerative_clustering
from .models.dbscan import dbscan
from .models.isolation_forest import isolation_forest
from .models.kmeans import kmeams
from .models.local_outlier_factor import local_outlier_factor
from .models.one_class_svm import one_class_svm
from .logging import getLoggers
from .utils import check_directory_writable, get_temp_dir, mkdir, validate_form, get_tmp_dir, create_ticket, \
    save_to_temp, uncompress_file


class OutputDirNotSet(Exception):
    pass


if getenv('OUTPUT_DIR') is None:
    raise OutputDirNotSet('Environment variable OUTPUT_DIR is not set.')


FILE_NOT_FOUND_MESSAGE = "File not found"

# Logging
mainLogger, accountLogger = getLoggers()

# OpenAPI documentation
spec = APISpec(
    title="Clustering and Outlier Detection API",
    version=getenv('VERSION'),
    info=dict(
        description="",
        contact={"email": "kpsarakis94@gmail.com"}
    ),
    externalDocs={"description": "GitHub", "url": "https://github.com/OpertusMundi/clustering-outliers-service"},
    openapi_version="3.0.2",
    plugins=[FlaskPlugin()],
)

# Initialize app
app = Flask(__name__, instance_relative_config=True, instance_path=getenv('INSTANCE_PATH'))
environment = getenv('FLASK_ENV')
if environment == 'testing' or environment == 'development':
    secret_key = environment
else:
    secret_key = getenv('SECRET_KEY') or open(getenv('SECRET_KEY_FILE')).read()
app.config.from_mapping(
    SECRET_KEY=secret_key,
    DATABASE=getenv('DATABASE'),
)


def executor_callback(future):
    """The callback function called when a job has completed."""
    ticket, result, job_type, success, comment = future.result()
    if result is not None:
        rel_path = datetime.now().strftime("%y%m%d")
        rel_path = path.join(rel_path, ticket)
        output_path: str = path.join(getenv('OUTPUT_DIR'), rel_path)
        mkdir(output_path)
        filepath = path.join(getenv('OUTPUT_DIR'), rel_path, "result.json")
        with open(filepath, 'w') as fp:
            json.dump(result, fp)
    else:
        filepath = None
    with app.app_context():
        dbc = db.get_db()
        db_result = dbc.execute('SELECT requested_time, filesize FROM tickets WHERE ticket = ?;', [ticket]).fetchone()
        time = db_result['requested_time']
        filesize = db_result['filesize']
        execution_time = round((datetime.now(timezone.utc) - time.replace(tzinfo=timezone.utc)).total_seconds(), 3)
        dbc.execute('UPDATE tickets SET result=?, success=?, status=1, execution_time=?, comment=? WHERE ticket=?;',
                    [filepath, success, execution_time, comment, ticket])
        dbc.commit()
        accountLogger(ticket=ticket, success=success, execution_start=time, execution_time=execution_time,
                      comment=comment, filesize=filesize)
        dbc.close()
        mainLogger.info(f'Processing of ticket: {ticket} is completed successfully')


# Ensure the instance folder exists and initialize application, db and executor.
mkdir(app.instance_path)
db.init_app(app)
executor = Executor(app)
executor.add_default_done_callback(executor_callback)

# Enable CORS
if getenv('CORS') is not None:
    if getenv('CORS')[0:1] == '[':
        origins = json.loads(getenv('CORS'))
    else:
        origins = getenv('CORS')
    cors = CORS(app, origins=origins)


class JobType(Enum):
    KMEANS = auto()
    DBSCAN = auto()
    AGGLO = auto()
    ISOFOREST = auto()
    LOCALOUTLIER = auto()
    SVM = auto()


@executor.job
def enqueue(ticket: str, src_path: str, form: FlaskForm, job_type: JobType) -> tuple:
    """Enqueue a job (in case requested response type is 'deferred')."""
    filesize = stat(src_path).st_size
    dbc = db.get_db()
    dbc.execute('INSERT INTO tickets (ticket, filesize) VALUES(?, ?);', [ticket, filesize])
    dbc.commit()
    dbc.close()
    mainLogger.info(f'Starting processing ticket: {ticket}')
    try:
        if job_type is JobType.KMEANS:
            result = kmeams(form, src_path)
        elif job_type is JobType.DBSCAN:
            result = dbscan(form, src_path)
        elif job_type is JobType.AGGLO:
            result = agglomerative_clustering(form, src_path)
        elif job_type is JobType.ISOFOREST:
            result = isolation_forest(form, src_path)
        elif job_type is JobType.LOCALOUTLIER:
            result = local_outlier_factor(form, src_path)
        elif job_type is JobType.SVM:
            result = one_class_svm(form, src_path)
        else:
            result = None
    except Exception as e:
        mainLogger.error(f'Processing of ticket: {ticket} failed')
        return ticket, None, 0, str(e)
    else:
        return ticket, result, job_type, 1, None


@app.route("/")
def index():
    """The index route, gives info about the API endpoints."""
    mainLogger.info('Generating OpenAPI document...')
    return make_response(spec.to_dict(), 200)


@app.route("/_health")
def health_check():
    """Perform basic health checks
    ---
    get:
      tags:
        - Health
      summary: Get health status
      description: 'Get health status'
      operationId: 'getHealth'
      responses:
        default:
          description: An object with status information
          content:
            application/json:
              schema:
                type: object
                properties:
                  status:
                    type: string
                    description: A status of 'OK' or 'FAILED'
                  reason:
                    type: string
                    description: the reason of failure (if failed)
                  detail:
                    type: string
                    description: more details on this failure (if failed)
              examples:
                example-1:
                  value: |-
                    {"status": "OK"}
    """
    mainLogger.info('Performing health checks...')
    # Check that temp directory is writable
    try:
        check_directory_writable(get_temp_dir())
    except Exception as exc:
        return make_response({'status': 'FAILED', 'reason': 'temp directory not writable', 'detail': str(exc)},
                             200)
    # Check that we can connect to our PostGIS backend
    try:
        dbc = db.get_db()
        dbc.execute('SELECT 1').fetchone()
    except Exception as exc:
        return make_response({'status': 'FAILED', 'reason': 'cannot connect to SQLite backend', 'detail': str(exc)},
                             200)
    return make_response({'status': 'OK'},
                         200)


@app.route("/kmeans/file", methods=["POST"])
def k_means_file():
    """Perform kmeans clustering to a geospatial file that is provided with the request
            ---
            post:
              summary: Perform kmeans clustering to a geospatial file that is provided with the request
              tags:
                - kmeans
              requestBody:
                required: true
                content:
                  multipart/form-data:
                    schema:
                      type: object
                      properties:
                        resource:
                          type: string
                          format: binary
                          description: The geospatial file.
                        resource_type:
                          type: string
                          enum: [csv, shp]
                          description: The geospatial file type
                        response:
                          type: string
                          enum: [prompt, deferred]
                          description: Determines whether the clustering process should be promptly initiated (*prompt*) or queued (*deferred*). In the first case, the response waits for the result, in the second the response is immediate returning a ticket corresponding to the request.
                        columns:
                          type: array
                          default: null
                          description: The columns to cluster
                        id_column:
                          type: string
                          description: The column that will serve as the id
                        k:
                          type: integer
                          description: The number of expected clusters, leave empty to calculate this automatically
                        dist_measure:
                          type: string
                          default: euclidean
                          description: The distance measure used
                      required:
                        - resource
                        - resource_type
              responses:
                200:
                  description: kmeans completed and returned.
                  content:
                      application/json:
                        schema:
                          type: object
                          properties:
                            cluster_centers:
                              type: array
                              description: The cluster centers
                            ids:
                              type: array
                              description: The row ids
                            labels:
                              type: array
                              description: The row labels
                202:
                  description: Accepted for processing, but clustering has not been completed.
                  content:
                    application/json:
                      schema:
                        type: object
                        properties:
                          ticket:
                            type: string
                            description: The ticket corresponding to the request.
                          endpoint:
                            type: string
                            description: The *resource* endpoint to get the resulting resource when ready.
                          status:
                            type: string
                            description: The *status* endpoint to poll for the status of the request.
                  links:
                    GetStatus:
                      operationId: getStatus
                      parameters:
                        ticket: '$response.body#/ticket'
                      description: The `ticket` value returned in the response can be used as the `ticket` parameter in `GET /status/{ticket}`.
                400:
                  description: Client error.
    """
    form = KMeansFileForm()
    validate_form(form, mainLogger)
    mainLogger.info(f"Starting /kmeans/file with file: {form.resource.data.filename}")
    tmp_dir: str = get_tmp_dir("clustering_outliers")
    ticket: str = create_ticket()
    src_file_path: str = save_to_temp(form, tmp_dir, ticket)
    src_file_path: str = uncompress_file(src_file_path)

    # Immediate results
    if form.response.data == "prompt":
        response = kmeams(form, src_file_path)
        return make_response(response, 200)
    # Wait for results
    else:
        enqueue.submit(ticket, src_file_path, form=form, job_type=JobType.KMEANS)
        response = {"ticket": ticket, "endpoint": f"/resource/{ticket}", "status": f"/status/{ticket}"}
        return make_response(response, 202)


@app.route("/kmeans/path", methods=["POST"])
def k_means_path():
    """Perform kmeans clustering to a geospatial file that its path provided with the request
            ---
            post:
              summary: Perform kmeans clustering to a geospatial file that its path provided with the request
              tags:
                - kmeans
              requestBody:
                required: true
                content:
                  application/x-www-form-urlencoded:
                    schema:
                      type: object
                      properties:
                        resource:
                          type: string
                          description: The path of the geospatial file.
                        resource_type:
                          type: string
                          enum: [csv, shp]
                          description: The geospatial file type
                        response:
                          type: string
                          enum: [prompt, deferred]
                          description: Determines whether the clustering process should be promptly initiated (*prompt*) or queued (*deferred*). In the first case, the response waits for the result, in the second the response is immediate returning a ticket corresponding to the request.
                        columns:
                          type: array
                          default: null
                          description: The columns to cluster
                        id_column:
                          type: string
                          description: The column that will serve as the id
                        k:
                          type: integer
                          description: The number of expected clusters, leave empty to calculate this automatically
                        dist_measure:
                          type: string
                          default: euclidean
                          description: The distance measure used
                      required:
                        - resource
                        - resource_type
              responses:
                200:
                  description: kmeans completed and returned.
                  content:
                      application/json:
                        schema:
                          type: object
                          properties:
                            cluster_centers:
                              type: array
                              description: The cluster centers
                            ids:
                              type: array
                              description: The row ids
                            labels:
                              type: array
                              description: The row labels
                202:
                  description: Accepted for processing, but clustering has not been completed.
                  content:
                    application/json:
                      schema:
                        type: object
                        properties:
                          ticket:
                            type: string
                            description: The ticket corresponding to the request.
                          endpoint:
                            type: string
                            description: The *resource* endpoint to get the resulting resource when ready.
                          status:
                            type: string
                            description: The *status* endpoint to poll for the status of the request.
                  links:
                    GetStatus:
                      operationId: getStatus
                      parameters:
                        ticket: '$response.body#/ticket'
                      description: The `ticket` value returned in the response can be used as the `ticket` parameter in `GET /status/{ticket}`.
                400:
                  description: Client error.
    """
    form = KMeansPathForm()
    validate_form(form, mainLogger)
    mainLogger.info(f"Starting /kmeans/path with file: {form.resource.data}")
    src_file_path: str = form.resource.data
    src_file_path: str = uncompress_file(src_file_path)

    if not path.exists(src_file_path):
        abort(400, FILE_NOT_FOUND_MESSAGE)

    # Immediate results
    if form.response.data == "prompt":
        response = kmeams(form, src_file_path)
        return make_response(response, 200)
    # Wait for results
    else:
        ticket: str = create_ticket()
        enqueue.submit(ticket, src_file_path, form=form, job_type=JobType.KMEANS)
        response = {"ticket": ticket, "endpoint": f"/resource/{ticket}", "status": f"/status/{ticket}"}
        return make_response(response, 202)


@app.route("/dbscan/file", methods=["POST"])
def dbscan_file():
    """Perform dbscan clustering to a geospatial file that is provided with the request
            ---
            post:
              summary: Perform dbscan clustering to a geospatial file that is provided with the request
              tags:
                - dbscan
              requestBody:
                required: true
                content:
                  multipart/form-data:
                    schema:
                      type: object
                      properties:
                        resource:
                          type: string
                          format: binary
                          description: The geospatial file.
                        resource_type:
                          type: string
                          enum: [csv, shp]
                          description: The geospatial file type
                        response:
                          type: string
                          enum: [prompt, deferred]
                          description: Determines whether the clustering process should be promptly initiated (*prompt*) or queued (*deferred*). In the first case, the response waits for the result, in the second the response is immediate returning a ticket corresponding to the request.
                        columns:
                          type: array
                          default: null
                          description: The columns to cluster
                        id_column:
                          type: string
                          description: The column that will serve as the id
                        epsilon:
                          type: float
                          description: The epsilon parameter of dbscan
                        min_samples:
                            type: integer
                            description: The minimum number of points required to form a dense region.
                        dist_measure:
                          type: string
                          default: euclidean
                          description: The distance measure used
                      required:
                        - resource
                        - resource_type
              responses:
                200:
                  description: dbscan completed and returned.
                  content:
                      application/json:
                        schema:
                          type: object
                          properties:
                            core_sample_indices:
                              type: array
                              description: The core sample indices
                            components:
                                type: array
                                description: The components
                            ids:
                              type: array
                              description: The row ids
                            labels:
                              type: array
                              description: The row labels
                202:
                  description: Accepted for processing, but clustering has not been completed.
                  content:
                    application/json:
                      schema:
                        type: object
                        properties:
                          ticket:
                            type: string
                            description: The ticket corresponding to the request.
                          endpoint:
                            type: string
                            description: The *resource* endpoint to get the resulting resource when ready.
                          status:
                            type: string
                            description: The *status* endpoint to poll for the status of the request.
                  links:
                    GetStatus:
                      operationId: getStatus
                      parameters:
                        ticket: '$response.body#/ticket'
                      description: The `ticket` value returned in the response can be used as the `ticket` parameter in `GET /status/{ticket}`.
                400:
                  description: Client error.
    """
    form = DBScanFileForm()
    validate_form(form, mainLogger)
    mainLogger.info(f"Starting /dbscan/file with file: {form.resource.data.filename}")
    tmp_dir: str = get_tmp_dir("clustering_outliers")
    ticket: str = create_ticket()
    src_file_path: str = save_to_temp(form, tmp_dir, ticket)
    src_file_path: str = uncompress_file(src_file_path)

    # Immediate results
    if form.response.data == "prompt":
        response = dbscan(form, src_file_path)
        return make_response(response, 200)
    # Wait for results
    else:
        enqueue.submit(ticket, src_file_path, form=form, job_type=JobType.DBSCAN)
        response = {"ticket": ticket, "endpoint": f"/resource/{ticket}", "status": f"/status/{ticket}"}
        return make_response(response, 202)


@app.route("/dbscan/path", methods=["POST"])
def dbscan_path():
    """Perform dbscan clustering to a geospatial file that its path is provided with the request
        ---
        post:
          summary: >-
            Perform dbscan clustering to a geospatial file that its path is provided
            with the request
          tags:
            - dbscan
          requestBody:
            required: true
            content:
              application/x-www-form-urlencoded:
                schema:
                  type: object
                  properties:
                    resource:
                      type: string
                      description: The geospatial file path.
                    resource_type:
                      type: string
                      enum:
                        - csv
                        - shp
                      description: The geospatial file type
                    response:
                      type: string
                      enum:
                        - prompt
                        - deferred
                      description: >-
                        Determines whether the clustering process should be promptly
                        initiated (*prompt*) or queued (*deferred*). In the first case,
                        the response waits for the result, in the second the response is
                        immediate returning a ticket corresponding to the request.
                    columns:
                      type: array
                      default: null
                      description: The columns to cluster
                    id_column:
                      type: string
                      description: The column that will serve as the id
                    epsilon:
                      type: float
                      description: The epsilon parameter of dbscan
                    min_samples:
                      type: integer
                      description: The minimum number of points required to form a dense region.
                    dist_measure:
                      type: string
                      default: euclidean
                      description: The distance measure used
                  required:
                    - resource
                    - resource_type
          responses:
            '200':
              description: dbscan completed and returned.
              content:
                application/json:
                  schema:
                    type: object
                    properties:
                      core_sample_indices:
                        type: array
                        description: The core sample indices
                      components:
                        type: array
                        description: The components
                      ids:
                        type: array
                        description: The row ids
                      labels:
                        type: array
                        description: The row labels
            '202':
              description: 'Accepted for processing, but clustering has not been completed.'
              content:
                application/json:
                  schema:
                    type: object
                    properties:
                      ticket:
                        type: string
                        description: The ticket corresponding to the request.
                      endpoint:
                        type: string
                        description: >-
                          The *resource* endpoint to get the resulting resource when
                          ready.
                      status:
                        type: string
                        description: The *status* endpoint to poll for the status of the request.
              links:
                GetStatus:
                  operationId: getStatus
                  parameters:
                    ticket: '$response.body#/ticket'
                  description: >-
                    The `ticket` value returned in the response can be used as the
                    `ticket` parameter in `GET /status/{ticket}`.
            '400':
              description: Client error.

    """
    form = DBScanPathForm()
    validate_form(form, mainLogger)
    mainLogger.info(f"Starting /dbscan/path with file: {form.resource.data}")
    src_file_path: str = form.resource.data
    src_file_path: str = uncompress_file(src_file_path)

    if not path.exists(src_file_path):
        abort(400, FILE_NOT_FOUND_MESSAGE)

    # Immediate results
    if form.response.data == "prompt":
        response = dbscan(form, src_file_path)
        return make_response(response, 200)
    # Wait for results
    else:
        ticket: str = create_ticket()
        enqueue.submit(ticket, src_file_path, form=form, job_type=JobType.DBSCAN)
        response = {"ticket": ticket, "endpoint": f"/resource/{ticket}", "status": f"/status/{ticket}"}
        return make_response(response, 202)


@app.route("/agglomerative/file", methods=["POST"])
def agglomerative_file():
    """Perform agglomerative clustering to a geospatial file that is provided with the request
            ---
            post:
              summary: Perform agglomerative clustering to a geospatial file that is provided with the request
              tags:
                - agglomerative
              requestBody:
                required: true
                content:
                  multipart/form-data:
                    schema:
                      type: object
                      properties:
                        resource:
                          type: string
                          format: binary
                          description: The geospatial file.
                        resource_type:
                          type: string
                          enum: [csv, shp]
                          description: The geospatial file type
                        response:
                          type: string
                          enum: [prompt, deferred]
                          description: Determines whether the clustering process should be promptly initiated (*prompt*) or queued (*deferred*). In the first case, the response waits for the result, in the second the response is immediate returning a ticket corresponding to the request.
                        columns:
                          type: array
                          default: null
                          description: The columns to cluster
                        id_column:
                          type: string
                          description: The column that will serve as the id
                        k:
                          type: integer
                          description: The number of clusters
                        linkage:
                            type: string
                            enum: [ward, complete, average, single]
                            description: The linkage type
                        dist_threshold:
                          type: string
                          default: euclidean
                          description: The distance measure used
                        dist_measure:
                          type: string
                          default: euclidean
                          description: The distance measure used
                      required:
                        - resource
                        - resource_type
              responses:
                200:
                  description: agglomerative clustering completed and returned.
                  content:
                      application/json:
                        schema:
                          type: object
                          properties:
                            n_clusters:
                              type: integer
                              description: The number of clusters
                            n_leaves:
                                type: integer
                                description: The number of leaves
                            n_connected_components:
                              type: integer
                              description: The number of connected components
                            children:
                              type: array
                              description: The children produced in the clustering process
                            ids:
                              type: array
                              description: The row ids
                            labels:
                              type: array
                              description: The row labels
                202:
                  description: Accepted for processing, but clustering has not been completed.
                  content:
                    application/json:
                      schema:
                        type: object
                        properties:
                          ticket:
                            type: string
                            description: The ticket corresponding to the request.
                          endpoint:
                            type: string
                            description: The *resource* endpoint to get the resulting resource when ready.
                          status:
                            type: string
                            description: The *status* endpoint to poll for the status of the request.
                  links:
                    GetStatus:
                      operationId: getStatus
                      parameters:
                        ticket: '$response.body#/ticket'
                      description: The `ticket` value returned in the response can be used as the `ticket` parameter in `GET /status/{ticket}`.
                400:
                  description: Client error.
    """
    form = AgglomerativeFileForm()
    validate_form(form, mainLogger)
    mainLogger.info(f"Starting /agglomerative/file with file: {form.resource.data.filename}")
    tmp_dir: str = get_tmp_dir("clustering_outliers")
    ticket: str = create_ticket()
    src_file_path: str = save_to_temp(form, tmp_dir, ticket)
    src_file_path: str = uncompress_file(src_file_path)

    # Immediate results
    if form.response.data == "prompt":
        response = agglomerative_clustering(form, src_file_path)
        return make_response(response, 200)
    # Wait for results
    else:
        enqueue.submit(ticket, src_file_path, form=form, job_type=JobType.AGGLO)
        response = {"ticket": ticket, "endpoint": f"/resource/{ticket}", "status": f"/status/{ticket}"}
        return make_response(response, 202)


@app.route("/agglomerative/path", methods=["POST"])
def agglomerative_path():
    """Perform agglomerative clustering to a geospatial file that its path is provided with the request
            ---
            post:
              summary: Perform agglomerative clustering to a geospatial file that its path is provided with the request
              tags:
                - agglomerative
              requestBody:
                required: true
                content:
                  application/x-www-form-urlencoded:
                    schema:
                      type: object
                      properties:
                        resource:
                          type: string
                          description: The geospatial file path.
                        resource_type:
                          type: string
                          enum: [csv, shp]
                          description: The geospatial file type
                        response:
                          type: string
                          enum: [prompt, deferred]
                          description: Determines whether the clustering process should be promptly initiated (*prompt*) or queued (*deferred*). In the first case, the response waits for the result, in the second the response is immediate returning a ticket corresponding to the request.
                        columns:
                          type: array
                          default: null
                          description: The columns to cluster
                        id_column:
                          type: string
                          description: The column that will serve as the id
                        k:
                          type: integer
                          description: The number of clusters
                        linkage:
                            type: string
                            enum: [ward, complete, average, single]
                            description: The linkage type
                        dist_threshold:
                          type: string
                          default: euclidean
                          description: The distance measure used
                        dist_measure:
                          type: string
                          default: euclidean
                          description: The distance measure used
                      required:
                        - resource
                        - resource_type
              responses:
                200:
                  description: agglomerative clustering completed and returned.
                  content:
                      application/json:
                        schema:
                          type: object
                          properties:
                            n_clusters:
                              type: integer
                              description: The number of clusters
                            n_leaves:
                                type: integer
                                description: The number of leaves
                            n_connected_components:
                              type: integer
                              description: The number of connected components
                            children:
                              type: array
                              description: The children produced in the clustering process
                            ids:
                              type: array
                              description: The row ids
                            labels:
                              type: array
                              description: The row labels
                202:
                  description: Accepted for processing, but clustering has not been completed.
                  content:
                    application/json:
                      schema:
                        type: object
                        properties:
                          ticket:
                            type: string
                            description: The ticket corresponding to the request.
                          endpoint:
                            type: string
                            description: The *resource* endpoint to get the resulting resource when ready.
                          status:
                            type: string
                            description: The *status* endpoint to poll for the status of the request.
                  links:
                    GetStatus:
                      operationId: getStatus
                      parameters:
                        ticket: '$response.body#/ticket'
                      description: The `ticket` value returned in the response can be used as the `ticket` parameter in `GET /status/{ticket}`.
                400:
                  description: Client error.
    """
    form = AgglomerativePathForm()
    validate_form(form, mainLogger)
    mainLogger.info(f"Starting /agglomerative/path with file: {form.resource.data}")
    src_file_path: str = form.resource.data
    src_file_path: str = uncompress_file(src_file_path)

    if not path.exists(src_file_path):
        abort(400, FILE_NOT_FOUND_MESSAGE)

    # Immediate results
    if form.response.data == "prompt":
        response = agglomerative_clustering(form, src_file_path)
        return make_response(response, 200)
    # Wait for results
    else:
        ticket: str = create_ticket()
        enqueue.submit(ticket, src_file_path, form=form, job_type=JobType.AGGLO)
        response = {"ticket": ticket, "endpoint": f"/resource/{ticket}", "status": f"/status/{ticket}"}
        return make_response(response, 202)


@app.route("/isolation_forest/file", methods=["POST"])
def isolation_forest_file():
    """Perform outlier detection with isolation forest to a geospatial file that is provided with the request
            ---
            post:
              summary: Perform outlier detection with isolation forest to a geospatial file that is provided with the request
              tags:
                - isolation_forest
              requestBody:
                required: true
                content:
                  multipart/form-data:
                    schema:
                      type: object
                      properties:
                        resource:
                          type: string
                          format: binary
                          description: The geospatial file.
                        resource_type:
                          type: string
                          enum: [csv, shp]
                          description: The geospatial file type
                        response:
                          type: string
                          enum: [prompt, deferred]
                          description: Determines whether the outlier detection process should be promptly initiated (*prompt*) or queued (*deferred*). In the first case, the response waits for the result, in the second the response is immediate returning a ticket corresponding to the request.
                        columns:
                          type: array
                          default: null
                          description: The columns to cluster
                        id_column:
                          type: string
                          description: The column that will serve as the id
                        epsilon:
                          type: float
                          description: The epsilon parameter of dbscan
                        min_samples:
                            type: integer
                            description: The minimum number of points required to form a dense region.
                        dist_measure:
                          type: string
                          default: euclidean
                          description: The distance measure used
                      required:
                        - resource
                        - resource_type
              responses:
                200:
                  description: isolation forest completed and returned.
                  content:
                      application/json:
                        schema:
                          type: object
                          properties:
                            outliers:
                              type: object
                              description: The detected outliers
                202:
                  description: Accepted for processing, but outlier detection has not been completed.
                  content:
                    application/json:
                      schema:
                        type: object
                        properties:
                          ticket:
                            type: string
                            description: The ticket corresponding to the request.
                          endpoint:
                            type: string
                            description: The *resource* endpoint to get the resulting resource when ready.
                          status:
                            type: string
                            description: The *status* endpoint to poll for the status of the request.
                  links:
                    GetStatus:
                      operationId: getStatus
                      parameters:
                        ticket: '$response.body#/ticket'
                      description: The `ticket` value returned in the response can be used as the `ticket` parameter in `GET /status/{ticket}`.
                400:
                  description: Client error.
    """
    form = IsoForestFileForm()
    validate_form(form, mainLogger)
    mainLogger.info(f"Starting /isolation_forest/file with file: {form.resource.data.filename}")
    tmp_dir: str = get_tmp_dir("clustering_outliers")
    ticket: str = create_ticket()
    src_file_path: str = save_to_temp(form, tmp_dir, ticket)
    src_file_path: str = uncompress_file(src_file_path)

    # Immediate results
    if form.response.data == "prompt":
        response = isolation_forest(form, src_file_path)
        return make_response(response, 200)
    # Wait for results
    else:
        enqueue.submit(ticket, src_file_path, form=form, job_type=JobType.ISOFOREST)
        response = {"ticket": ticket, "endpoint": f"/resource/{ticket}", "status": f"/status/{ticket}"}
        return make_response(response, 202)


@app.route("/isolation_forest/path", methods=["POST"])
def isolation_forest_path():
    """Perform outlier detection with isolation forest to a geospatial file that its path is provided with the request
            ---
            post:
              summary: Perform outlier detection with isolation forest to a geospatial file that its path is provided with the request
              tags:
                - isolation_forest
              requestBody:
                required: true
                content:
                  application/x-www-form-urlencoded:
                    schema:
                      type: object
                      properties:
                        resource:
                          type: string
                          description: The geospatial file path.
                        resource_type:
                          type: string
                          enum: [csv, shp]
                          description: The geospatial file type
                        response:
                          type: string
                          enum: [prompt, deferred]
                          description: Determines whether the outlier detection process should be promptly initiated (*prompt*) or queued (*deferred*). In the first case, the response waits for the result, in the second the response is immediate returning a ticket corresponding to the request.
                        columns:
                          type: array
                          default: null
                          description: The columns to cluster
                        id_column:
                          type: string
                          description: The column that will serve as the id
                        n_estimators:
                          type: integer
                          description: The number of estimators
                        max_samples:
                            type: integer
                            description: The maximum samples
                      required:
                        - resource
                        - resource_type
              responses:
                200:
                  description: isolation forest completed and returned.
                  content:
                      application/json:
                        schema:
                          type: object
                          properties:
                            outliers:
                              type: object
                              description: The detected outliers
                202:
                  description: Accepted for processing, but outlier detection has not been completed.
                  content:
                    application/json:
                      schema:
                        type: object
                        properties:
                          ticket:
                            type: string
                            description: The ticket corresponding to the request.
                          endpoint:
                            type: string
                            description: The *resource* endpoint to get the resulting resource when ready.
                          status:
                            type: string
                            description: The *status* endpoint to poll for the status of the request.
                  links:
                    GetStatus:
                      operationId: getStatus
                      parameters:
                        ticket: '$response.body#/ticket'
                      description: The `ticket` value returned in the response can be used as the `ticket` parameter in `GET /status/{ticket}`.
                400:
                  description: Client error.
    """
    form = IsoForestPathForm()
    validate_form(form, mainLogger)
    mainLogger.info(f"Starting /isolation_forest/path with file: {form.resource.data}")
    src_file_path: str = form.resource.data
    src_file_path: str = uncompress_file(src_file_path)

    if not path.exists(src_file_path):
        abort(400, FILE_NOT_FOUND_MESSAGE)

    # Immediate results
    if form.response.data == "prompt":
        response = isolation_forest(form, src_file_path)
        return make_response(response, 200)
    # Wait for results
    else:
        ticket: str = create_ticket()
        enqueue.submit(ticket, src_file_path, form=form, job_type=JobType.ISOFOREST)
        response = {"ticket": ticket, "endpoint": f"/resource/{ticket}", "status": f"/status/{ticket}"}
        return make_response(response, 202)


@app.route("/local_outlier_factor/file", methods=["POST"])
def local_outlier_factor_file():
    """Perform local outlier factor anomaly detection to a geospatial file that is provided with the request
            ---
            post:
              summary: Perform local outlier factor anomaly detection to a geospatial file that is provided with the request
              tags:
                - local_outlier_factor
              requestBody:
                required: true
                content:
                  multipart/form-data:
                    schema:
                      type: object
                      properties:
                        resource:
                          type: string
                          format: binary
                          description: The geospatial file.
                        resource_type:
                          type: string
                          enum: [csv, shp]
                          description: The geospatial file type
                        response:
                          type: string
                          enum: [prompt, deferred]
                          description: Determines whether the outlier detection process should be promptly initiated (*prompt*) or queued (*deferred*). In the first case, the response waits for the result, in the second the response is immediate returning a ticket corresponding to the request.
                        columns:
                          type: array
                          default: null
                          description: The columns to cluster
                        id_column:
                          type: string
                          description: The column that will serve as the id
                        n_neighbors:
                          type: integer
                          description: The number of neighbors
                      required:
                        - resource
                        - resource_type
              responses:
                200:
                  description: local outlier factor completed and returned.
                  content:
                      application/json:
                        schema:
                          type: object
                          properties:
                            outliers:
                              type: object
                              description: The detected outliers
                202:
                  description: Accepted for processing, but outlier detection has not been completed.
                  content:
                    application/json:
                      schema:
                        type: object
                        properties:
                          ticket:
                            type: string
                            description: The ticket corresponding to the request.
                          endpoint:
                            type: string
                            description: The *resource* endpoint to get the resulting resource when ready.
                          status:
                            type: string
                            description: The *status* endpoint to poll for the status of the request.
                  links:
                    GetStatus:
                      operationId: getStatus
                      parameters:
                        ticket: '$response.body#/ticket'
                      description: The `ticket` value returned in the response can be used as the `ticket` parameter in `GET /status/{ticket}`.
                400:
                  description: Client error.
    """
    form = LOFFileForm()
    validate_form(form, mainLogger)
    mainLogger.info(f"Starting /local_outlier_factor/file with file: {form.resource.data.filename}")
    tmp_dir: str = get_tmp_dir("clustering_outliers")
    ticket: str = create_ticket()
    src_file_path: str = save_to_temp(form, tmp_dir, ticket)
    src_file_path: str = uncompress_file(src_file_path)

    # Immediate results
    if form.response.data == "prompt":
        response = local_outlier_factor(form, src_file_path)
        return make_response(response, 200)
    # Wait for results
    else:
        enqueue.submit(ticket, src_file_path, form=form, job_type=JobType.LOCALOUTLIER)
        response = {"ticket": ticket, "endpoint": f"/resource/{ticket}", "status": f"/status/{ticket}"}
        return make_response(response, 202)


@app.route("/local_outlier_factor/path", methods=["POST"])
def local_outlier_factor_path():
    """Perform local outlier factor anomaly detection to a geospatial file that its path is provided with the request
            ---
            post:
              summary: Perform local outlier factor anomaly detection to a geospatial file that its path is provided with the request
              tags:
                - local_outlier_factor
              requestBody:
                required: true
                content:
                  application/x-www-form-urlencoded:
                    schema:
                      type: object
                      properties:
                        resource:
                          type: string
                          description: The geospatial file path.
                        resource_type:
                          type: string
                          enum: [csv, shp]
                          description: The geospatial file type
                        response:
                          type: string
                          enum: [prompt, deferred]
                          description: Determines whether the outlier detection process should be promptly initiated (*prompt*) or queued (*deferred*). In the first case, the response waits for the result, in the second the response is immediate returning a ticket corresponding to the request.
                        columns:
                          type: array
                          default: null
                          description: The columns to cluster
                        id_column:
                          type: string
                          description: The column that will serve as the id
                        n_neighbors:
                          type: integer
                          description: The number of neighbors
                      required:
                        - resource
                        - resource_type
              responses:
                200:
                  description: local outlier factor completed and returned.
                  content:
                      application/json:
                        schema:
                          type: object
                          properties:
                            outliers:
                              type: object
                              description: The detected outliers
                202:
                  description: Accepted for processing, but outlier detection has not been completed.
                  content:
                    application/json:
                      schema:
                        type: object
                        properties:
                          ticket:
                            type: string
                            description: The ticket corresponding to the request.
                          endpoint:
                            type: string
                            description: The *resource* endpoint to get the resulting resource when ready.
                          status:
                            type: string
                            description: The *status* endpoint to poll for the status of the request.
                  links:
                    GetStatus:
                      operationId: getStatus
                      parameters:
                        ticket: '$response.body#/ticket'
                      description: The `ticket` value returned in the response can be used as the `ticket` parameter in `GET /status/{ticket}`.
                400:
                  description: Client error.
    """
    form = LOFPathForm()
    validate_form(form, mainLogger)
    mainLogger.info(f"Starting /local_outlier_factor/path with file: {form.resource.data}")
    src_file_path: str = form.resource.data
    src_file_path: str = uncompress_file(src_file_path)

    if not path.exists(src_file_path):
        abort(400, FILE_NOT_FOUND_MESSAGE)

    # Immediate results
    if form.response.data == "prompt":
        response = local_outlier_factor(form, src_file_path)
        return make_response(response, 200)
    # Wait for results
    else:
        ticket: str = create_ticket()
        enqueue.submit(ticket, src_file_path, form=form, job_type=JobType.LOCALOUTLIER)
        response = {"ticket": ticket, "endpoint": f"/resource/{ticket}", "status": f"/status/{ticket}"}
        return make_response(response, 202)


@app.route("/one_class_svm/file", methods=["POST"])
def svm_file():
    """Perform one class svm anomaly detection to a geospatial file that is provided with the request
            ---
            post:
              summary: Perform one class svm anomaly detection to a geospatial file that is provided with the request
              tags:
                - one_class_svm
              requestBody:
                required: true
                content:
                  multipart/form-data:
                    schema:
                      type: object
                      properties:
                        resource:
                          type: string
                          format: binary
                          description: The geospatial file.
                        resource_type:
                          type: string
                          enum: [csv, shp]
                          description: The geospatial file type
                        response:
                          type: string
                          enum: [prompt, deferred]
                          description: Determines whether the outlier detection process should be promptly initiated (*prompt*) or queued (*deferred*). In the first case, the response waits for the result, in the second the response is immediate returning a ticket corresponding to the request.
                        columns:
                          type: array
                          default: null
                          description: The columns to cluster
                        id_column:
                          type: string
                          description: The column that will serve as the id
                        degree:
                          type: integer
                          description: One class svm degree
                      required:
                        - resource
                        - resource_type
              responses:
                200:
                  description: one class svm completed and returned.
                  content:
                      application/json:
                        schema:
                          type: object
                          properties:
                            outliers:
                              type: object
                              description: The detected outliers
                202:
                  description: Accepted for processing, but outlier detection has not been completed.
                  content:
                    application/json:
                      schema:
                        type: object
                        properties:
                          ticket:
                            type: string
                            description: The ticket corresponding to the request.
                          endpoint:
                            type: string
                            description: The *resource* endpoint to get the resulting resource when ready.
                          status:
                            type: string
                            description: The *status* endpoint to poll for the status of the request.
                  links:
                    GetStatus:
                      operationId: getStatus
                      parameters:
                        ticket: '$response.body#/ticket'
                      description: The `ticket` value returned in the response can be used as the `ticket` parameter in `GET /status/{ticket}`.
                400:
                  description: Client error.
    """
    form = OCSVMFileForm()
    validate_form(form, mainLogger)
    mainLogger.info(f"Starting /one_class_svm/file with file: {form.resource.data.filename}")
    tmp_dir: str = get_tmp_dir("clustering_outliers")
    ticket: str = create_ticket()
    src_file_path: str = save_to_temp(form, tmp_dir, ticket)
    src_file_path: str = uncompress_file(src_file_path)

    # Immediate results
    if form.response.data == "prompt":
        response = one_class_svm(form, src_file_path)
        return make_response(response, 200)
    # Wait for results
    else:
        enqueue.submit(ticket, src_file_path, form=form, job_type=JobType.SVM)
        response = {"ticket": ticket, "endpoint": f"/resource/{ticket}", "status": f"/status/{ticket}"}
        return make_response(response, 202)


@app.route("/one_class_svm/path", methods=["POST"])
def svm_path():
    """Perform one class svm anomaly detection to a geospatial file that its path is provided with the request
            ---
            post:
              summary: Perform one class svm anomaly detection to a geospatial file that its path is provided with the request
              tags:
                - one_class_svm
              requestBody:
                required: true
                content:
                  application/x-www-form-urlencoded:
                    schema:
                      type: object
                      properties:
                        resource:
                          type: string
                          description: The geospatial file path.
                        resource_type:
                          type: string
                          enum: [csv, shp]
                          description: The geospatial file type
                        response:
                          type: string
                          enum: [prompt, deferred]
                          description: Determines whether the outlier detection process should be promptly initiated (*prompt*) or queued (*deferred*). In the first case, the response waits for the result, in the second the response is immediate returning a ticket corresponding to the request.
                        columns:
                          type: array
                          default: null
                          description: The columns to cluster
                        id_column:
                          type: string
                          description: The column that will serve as the id
                        degree:
                          type: integer
                          description: One class svm degree
                      required:
                        - resource
                        - resource_type
              responses:
                200:
                  description: one class svm completed and returned.
                  content:
                      application/json:
                        schema:
                          type: object
                          properties:
                            outliers:
                              type: object
                              description: The detected outliers
                202:
                  description: Accepted for processing, but outlier detection has not been completed.
                  content:
                    application/json:
                      schema:
                        type: object
                        properties:
                          ticket:
                            type: string
                            description: The ticket corresponding to the request.
                          endpoint:
                            type: string
                            description: The *resource* endpoint to get the resulting resource when ready.
                          status:
                            type: string
                            description: The *status* endpoint to poll for the status of the request.
                  links:
                    GetStatus:
                      operationId: getStatus
                      parameters:
                        ticket: '$response.body#/ticket'
                      description: The `ticket` value returned in the response can be used as the `ticket` parameter in `GET /status/{ticket}`.
                400:
                  description: Client error.
    """
    form = OCSVMPathForm()
    validate_form(form, mainLogger)
    mainLogger.info(f"Starting /one_class_svm/path with file: {form.resource.data}")
    src_file_path: str = form.resource.data
    src_file_path: str = uncompress_file(src_file_path)

    if not path.exists(src_file_path):
        abort(400, FILE_NOT_FOUND_MESSAGE)

    # Immediate results
    if form.response.data == "prompt":
        response = one_class_svm(form, src_file_path)
        return make_response(response, 200)
    # Wait for results
    else:
        ticket: str = create_ticket()
        enqueue.submit(ticket, src_file_path, form=form, job_type=JobType.SVM)
        response = {"ticket": ticket, "endpoint": f"/resource/{ticket}", "status": f"/status/{ticket}"}
        return make_response(response, 202)


@app.route("/status/<ticket>")
def status(ticket):
    """Get the status of a specific ticket.
    ---
    get:
      summary: Get the status of a task request.
      operationId: getStatus
      description: Returns the status of a request corresponding to a specific ticket.
      tags:
        - Status
      parameters:
        - name: ticket
          in: path
          description: The ticket of the request
          required: true
          schema:
            type: string
      responses:
        200:
          description: Ticket found and status returned.
          content:
            application/json:
              schema:
                type: object
                properties:
                  completed:
                    type: boolean
                    description: Whether profiling process has been completed or not.
                  success:
                    type: boolean
                    description: Whether profiling process completed successfully.
                  comment:
                    type: string
                    description: If profiling has failed, a short comment describing the reason.
                  requested:
                    type: string
                    format: datetime
                    description: The timestamp of the request.
                  execution_time(s):
                    type: integer
                    description: The execution time in seconds.
        404:
          description: Ticket not found.
    """
    if ticket is None:
        return make_response('Ticket is missing.', 400)
    dbc = db.get_db()
    results = dbc.execute(
        'SELECT status, success, requested_time, execution_time, comment FROM tickets WHERE ticket = ?',
        [ticket]).fetchone()
    if results is not None:
        if results['success'] is not None:
            success = bool(results['success'])
        else:
            success = None
        return make_response({"completed": bool(results['status']), "success": success,
                              "requested": results['requested_time'], "execution_time(s)": results['execution_time'],
                              "comment": results['comment']}, 200)
    return make_response('Not found.', 404)


@app.route("/resource/<ticket>")
def resource(ticket):
    """Get the resulted resource associated with a specific ticket.
    ---
    get:
      summary: Get the resource associated to a task request.
      description: Returns the resource resulted from a task request corresponding to a specific ticket.
      tags:
        - Resource
      parameters:
        - name: ticket
          in: path
          description: The ticket of the request
          required: true
          schema:
            type: string
      responses:
        200:
          description: The compressed spatial file.
          content:
            application/x-tar:
              schema:
                type: string
                format: binary
        404:
          description: Ticket not found or task has not been completed.
        507:
          description: Resource does not exist.
    """
    if ticket is None:
        return make_response('Resource ticket is missing.', 400)
    dbc = db.get_db()
    rel_path = dbc.execute('SELECT result FROM tickets WHERE ticket = ?', [ticket]).fetchone()['result']
    if rel_path is None:
        return make_response('Not found.', 404)
    file = path.join(getenv('OUTPUT_DIR'), rel_path)
    if not path.isfile(file):
        return make_response('Resource does not exist.', 507)
    return send_file(file, as_attachment=True)


# Views
with app.test_request_context():
    spec.path(view=svm_path)
    spec.path(view=svm_file)
    spec.path(view=agglomerative_path)
    spec.path(view=agglomerative_file)
    spec.path(view=dbscan_path)
    spec.path(view=dbscan_file)
    spec.path(view=isolation_forest_path)
    spec.path(view=isolation_forest_file)
    spec.path(view=k_means_file)
    spec.path(view=k_means_path)
    spec.path(view=local_outlier_factor_path)
    spec.path(view=local_outlier_factor_file)
    spec.path(view=status)
    spec.path(view=resource)

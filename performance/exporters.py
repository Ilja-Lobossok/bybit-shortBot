"""Single-attempt OTLP/HTTPS logs. A partial_success response is never acknowledged."""
import base64
import json
import time
from monitoring.otlp import CloudCredentials
from performance.storage import encoded


class LogExporter:
    def __init__(self, env, *, session=None):
        credentials = CloudCredentials.from_env(dict(env, GRAFANA_CLOUD_API_TOKEN=env.get("PERFORMANCE_GRAFANA_LOGS_TOKEN", "")))
        self.endpoint = credentials.endpoint.removesuffix("/metrics") + "/logs"
        self.authorization = "Basic " + base64.b64encode(
            (credentials.username + ":" + credentials.token).encode()).decode()
        self.environment = env.get("MONITORING_ENVIRONMENT", "production")
        self.account = env.get("PERFORMANCE_ACCOUNT_ALIAS", "primary")
        if session is None:
            import requests
            session = requests.Session()
            session.trust_env = False
        self.session = session

    def send(self, batch):
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest, ExportLogsServiceResponse
        from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
        request = ExportLogsServiceRequest()
        resource_logs = request.resource_logs.add()
        for key, value in {"service.name": "bybit-performance", "environment": self.environment,
                           "account_alias": self.account}.items():
            resource_logs.resource.attributes.append(KeyValue(key=key, value=AnyValue(string_value=value)))
        logs = resource_logs.scope_logs.add()
        logs.scope.name = "bybit_shortbot.performance"
        for row in batch["rows"]:
            record = logs.log_records.add()
            record.time_unix_nano = batch["created_ms"] * 1_000_000
            record.observed_time_unix_nano = record.time_unix_nano
            record.body.string_value = encoded(row)
        payload = request.SerializeToString()
        if len(payload) > 2 * 1024**2:
            return False
        try:
            response = self.session.post(self.endpoint, data=payload,
                                         headers={"Authorization": self.authorization, "Content-Type": "application/x-protobuf"},
                                         timeout=(2, 5), allow_redirects=False, stream=True)
            with response:
                if response.status_code != 200:
                    return False
                content = bytearray()
                deadline = time.monotonic() + 10
                for chunk in response.iter_content(4096):
                    content.extend(chunk)
                    if len(content) > 65536 or time.monotonic() > deadline:
                        return False
                reply = ExportLogsServiceResponse.FromString(bytes(content))
                return reply.partial_success.rejected_log_records == 0 and not reply.partial_success.error_message
        except Exception:
            return False

    def close(self):
        self.session.close()

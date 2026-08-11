from ip_proxy_pool.models import ProxyRecord


def encode_record(record: ProxyRecord) -> str:
    return record.model_dump_json()


def decode_record(raw: str | bytes) -> ProxyRecord:
    return ProxyRecord.model_validate_json(raw)

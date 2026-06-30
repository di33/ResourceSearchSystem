import io

from app.services.ks3_storage import KS3Storage


class _FakeS3:
    def __init__(self):
        self.put_calls = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        return {"ETag": '"etag"'}

    def head_object(self, Bucket, Key):
        return {"ContentLength": len(self.put_calls[-1]["Body"]), "ETag": '"etag"'}


def test_upload_fileobj_uses_put_object_with_content_length():
    s3 = _FakeS3()
    storage = KS3Storage(s3)

    size, etag, md5 = storage.upload_fileobj("downloads/res/pkg.zip", io.BytesIO(b"payload"), "application/zip")

    assert size == 7
    assert etag == '"etag"'
    assert md5 == "321c3cf486ed509164edec1e1981fec8"
    assert s3.put_calls == [
        {
            "Bucket": storage.bucket,
            "Key": "downloads/res/pkg.zip",
            "Body": b"payload",
            "ContentLength": 7,
            "ContentType": "application/zip",
        }
    ]

import hashlib
import json
import logging
import re
from typing import BinaryIO

import boto3
from botocore.exceptions import ClientError

from bed.core.models import CacheGrant

GRANT_SECONDS = 3600
OBJECT_ACTIONS = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
log = logging.getLogger(__name__)
MISS_CODES = frozenset({"NoSuchKey", "404"})
DENIED_CODES = frozenset({"AccessDenied", "403"})


def cache_key(repo: str) -> str:
    digest = hashlib.sha256(repo.encode()).hexdigest()[:16]
    slug = re.sub(r"[^A-Za-z0-9]+", "-", repo).strip("-")[:40]
    return f"repos/{digest}-{slug}/dependencies.tar.zst"


class StsCacheGrants:
    def __init__(self, bucket: str, role_arn: str):
        self.bucket = bucket
        self.role_arn = role_arn
        self.client = boto3.client("sts")

    def grant(self, repo: str) -> CacheGrant:
        key = cache_key(repo)
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": OBJECT_ACTIONS,
                    "Resource": f"arn:aws:s3:::{self.bucket}/{key}",
                },
            ],
        }
        response = self.client.assume_role(
            RoleArn=self.role_arn,
            RoleSessionName=f"cache-{hashlib.sha256(repo.encode()).hexdigest()[:16]}",
            Policy=json.dumps(policy),
            DurationSeconds=GRANT_SECONDS,
        )
        credentials = response["Credentials"]
        return CacheGrant(
            bucket=self.bucket,
            key=key,
            access_key_id=credentials["AccessKeyId"],
            secret_access_key=credentials["SecretAccessKey"],
            session_token=credentials["SessionToken"],
        )


class S3CacheStore:
    def __init__(self, grant: CacheGrant):
        self.bucket = grant.bucket
        self.key = grant.key
        self.client = boto3.client(
            "s3",
            aws_access_key_id=grant.access_key_id,
            aws_secret_access_key=grant.secret_access_key,
            aws_session_token=grant.session_token,
        )

    def open(self) -> tuple[dict[str, str], BinaryIO] | None:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.key)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in MISS_CODES:
                return None
            if code in DENIED_CODES:
                log.info("dependency cache read answered %s: no archive yet, or the cache role or S3 endpoint policy "
                         "denies the read", code)
                return None
            raise
        return dict(response.get("Metadata", {})), response["Body"]

    def upload(self, source: BinaryIO, metadata: dict[str, str]) -> None:
        self.client.upload_fileobj(source, self.bucket, self.key, ExtraArgs={"Metadata": metadata})

    def delete(self) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self.key)

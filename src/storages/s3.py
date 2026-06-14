from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from botocore.client import Config
import boto3


@dataclass
class ObjectMeta:
    """S3 对象元数据（不含 body）。

    可通过 :meth:`IS3Client.head` 获取。对象不存在时 ``head`` 返回 ``None``。
    """

    content_length: int
    """对象大小，单位字节。"""

    content_type: str | None = None
    """MIME 类型，如 ``image/png``。"""

    etag: str | None = None
    """实体标签（ETag）。单段上传时为内容 MD5 哈希；分片上传时为复合校验和。"""

    last_modified: datetime | None = None
    """对象最后修改时间。"""

    metadata: dict[str, str] | None = None
    """用户自定义元数据（x-amz-meta-*），键名已转为小写。"""


class IS3Client:
    """S3 兼容存储的抽象接口。

    定义对象存储操作的标准契约：元数据查询、读取、上传、下载、删除、
    资源释放及上下文管理器协议。具体实现需继承此类并覆盖所有抽象方法。
    """

    def head(self, bucket: str, object_key: str) -> ObjectMeta | None:
        """获取对象元数据，不下载 body。

        一次 HEAD 请求即可获取大小、MIME 类型、ETag、修改时间及自定义元数据，
        同时兼作存在性检查——对象不存在时返回 ``None``。

        :param bucket:      S3 桶名称。
        :param object_key:  对象的键（路径）。
        :return:            :class:`ObjectMeta`，对象不存在时返回 ``None``。
        """
        raise NotImplementedError

    def upload(self, bucket: str, object_key: str, local_file_path: Path | str):
        """将本地文件上传到 S3 兼容存储。

        :param bucket:          目标桶名称。
        :param object_key:      对象在桶中的键（路径）。
        :param local_file_path: 本地文件的绝对或相对路径。
        """
        raise NotImplementedError

    def get(self, bucket: str, object_key: str) -> bytes:
        """从 S3 兼容存储读取对象内容（全部读入内存）。

        大文件请使用 :meth:`get_stream` 以避免 OOM。

        :param bucket:     源桶名称。
        :param object_key: 对象的键（路径）。
        :return:           对象内容的字节数据。
        """
        raise NotImplementedError

    def get_stream(self, bucket: str, object_key: str, chunk_size: int = 8 * 1024 * 1024) -> Iterator[bytes]:
        """流式读取对象内容，逐块返回，不落盘。

        每次迭代从远端拉取至多 *chunk_size* 字节，内存在同一时刻
        只持有一个块，适合 GB 级大文件顺序处理。

        :param bucket:     源桶名称。
        :param object_key: 对象的键（路径）。
        :param chunk_size: 每个块的字节数，默认 8 MiB。
        :yield:            对象内容的字节块。
        """
        raise NotImplementedError

    def download(self, bucket: str, object_key: str, save_to: Path | str):
        """从 S3 兼容存储下载对象到本地文件。

        :param bucket:     源桶名称。
        :param object_key: 对象的键（路径）。
        :param save_to:    本地保存路径。
        """
        raise NotImplementedError

    def delete(self, bucket: str, object_key: str):
        """从 S3 兼容存储中删除对象。

        :param bucket:     桶名称。
        :param object_key: 对象的键（路径）。
        """
        raise NotImplementedError

    def close(self):
        """关闭底层客户端，释放连接池等资源。"""
        raise NotImplementedError

    def __enter__(self):
        raise NotImplementedError

    def __exit__(self, exc_type, exc, tb):
        raise NotImplementedError


class CommonS3Client(IS3Client):
    """基于 boto3 的通用 S3 兼容存储客户端实现。

    封装了 S3 兼容协议的基本操作（元数据查询、读取、上传、下载、删除），
    支持通过 botocore Config 对连接池、重试等行为进行细粒度控制。
    客户端实例线程安全，可在多线程上下文中复用。

    批量上传请使用 :func:`tools.storages.helpers.upload_batch`::

        from tools.storages.helpers import upload_batch
        upload_batch(client, tasks, max_workers=30)

    用法::

        client = CommonS3Client(endpoint_url="https://s3.example.com", ...)
        with client:
            client.upload("my-bucket", "path/to/obj", "/local/file.png")
    """

    def __init__(
        self,
        endpoint_url: str,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        config: Config | None = None,
    ):
        self.__endpoint_url = endpoint_url
        self.__config = config
        self.__s3_client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            config=config,
        )

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def head(self, bucket: str, object_key: str) -> ObjectMeta | None:
        """获取对象元数据，不下载 body。

        一次 HEAD 请求即可拿到大小、MIME 类型、ETag、修改时间及
        用户自定义元数据。对象不存在时返回 ``None``。

        :param bucket:      S3 桶名称。
        :param object_key:  对象的键（路径）。
        :return:            :class:`ObjectMeta`，对象不存在时返回 ``None``。
        """
        try:
            resp = self.__s3_client.head_object(Bucket=bucket, Key=object_key)
        except Exception:
            return None
        return ObjectMeta(
            content_length=resp["ContentLength"],
            content_type=resp.get("ContentType"),
            etag=resp.get("ETag", "").strip('"'),
            last_modified=resp.get("LastModified"),
            metadata=resp.get("Metadata"),
        )

    def get(self, bucket: str, object_key: str) -> bytes:
        response = self.__s3_client.get_object(Bucket=bucket, Key=object_key)
        return response["Body"].read()

    def get_stream(self, bucket: str, object_key: str, chunk_size: int = 8 * 1024 * 1024) -> Iterator[bytes]:
        response = self.__s3_client.get_object(Bucket=bucket, Key=object_key)
        body = response["Body"]
        for chunk in body.iter_chunks(chunk_size=chunk_size):
            yield chunk

    def upload(self, bucket: str, object_key: str, local_file_path: Path | str):
        try:
            self.__s3_client.upload_file(str(local_file_path), bucket, object_key)
        except Exception as e:
            print(f"\n❌ 上传失败: {local_file_path} | 错误: {e}")

    def download(self, bucket: str, object_key: str, save_to: Path | str):
        self.__s3_client.download_file(bucket, object_key, str(save_to))

    def delete(self, bucket: str, object_key: str):
        self.__s3_client.delete_object(Bucket=bucket, Key=object_key)

    def close(self):
        self.__s3_client.close()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()

    # ------------------------------------------------------------------ #
    #  Properties                                                         #
    # ------------------------------------------------------------------ #

    @property
    def endpoint_url(self) -> str:
        """S3 endpoint URL"""
        return self.__endpoint_url

    @property
    def config(self) -> Config | None:
        """boto3 client Config（只读）"""
        return self.__config

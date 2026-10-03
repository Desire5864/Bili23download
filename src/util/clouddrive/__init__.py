"""CloudDrive2 接入（gRPC-Web）。

`clouddrive_pb2` 是按官方 proto 现生成的，**别手改** —— 它对应 CD2 1.1.1 的接口
（生成的代码里那句 ValidateProtobufRuntimeVersion 要求的运行时版本是 7.35.1，
与 requirements.txt 里钉住的 protobuf 同名同版本，改动前先对齐这两处）。

CD2 升级后重新生成：

    curl -o clouddrive.proto https://www.clouddrive2.com/api/clouddrive.proto
    pip install grpcio-tools
    python -m grpc_tools.protoc -I<proto 所在目录> -I<grpcio_tools/_proto> \\
        --python_out=src/util/clouddrive clouddrive.proto

只需要 --python_out：传输是自己实现的（见 client.py），用不上生成的 _pb2_grpc。
"""

from .client import (
    CloudDriveAuthError,
    CloudDriveClient,
    CloudDriveError,
    status_label,
)

__all__ = [
    "CloudDriveClient",
    "CloudDriveError",
    "CloudDriveAuthError",
    "status_label",
]

import pyarrow.fs as fs
import os
import posixpath

src = fs.S3FileSystem(access_key=os.environ["S3_AK"], secret_key=os.environ["S3_SK"],
                      endpoint_override="http://minio.minio.svc.cluster.local:9000",
                      scheme="http")
infos = [i for i in src.get_file_info(fs.FileSelector("test-parquet", recursive=True))
         if i.type == fs.FileType.File and not i.path.startswith("test-parquet/demo/")]
total = 0
for i in infos:
    rel = i.path[len("test-parquet/"):]
    dst = posixpath.join("/data", rel)
    os.makedirs(posixpath.dirname(dst), exist_ok=True)
    with src.open_input_stream(i.path) as s, open(dst, "wb") as o:
        while True:
            chunk = s.read(4194304)
            if not chunk:
                break
            o.write(chunk)
            total += len(chunk)
    print("copied", rel)
print("DONE objects=%d bytes=%d" % (len(infos), total))

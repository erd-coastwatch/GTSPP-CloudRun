"""Read only NetCDF3 headers/string columns to build GTSPP subset JSON.

Avoid downloading observation arrays from the entire historical archive.
Supports classic CDF-1/CDF-2 files, the format produced by Roy and this port.
"""
import json
import struct
import numpy as np

NAMES = ("trajectory", "org", "type", "platform", "cruise")
TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 4, 6: 8}


class Header:
    def __init__(self, data):
        self.data = data
        self.pos = 4
        if data[:4] not in (b"CDF\x01", b"CDF\x02"):
            raise ValueError("Subset builder requires classic NetCDF3 (CDF-1/CDF-2)")
        self.version = data[3]
        self.u32()  # numrecs
        tag, count = self.u32(), self.u32()
        if tag not in (0, 10):
            raise ValueError("Invalid NetCDF dimension list")
        self.dims = []
        for _ in range(count):
            self.string()
            self.dims.append(self.u32())
        self.attributes()
        tag, count = self.u32(), self.u32()
        if tag != 11:
            raise ValueError("Missing NetCDF variables")
        self.variables = {}
        for _ in range(count):
            name = self.string()
            dims = [self.u32() for _ in range(self.u32())]
            self.attributes()
            datatype = self.u32()
            self.u32()  # padded vsize
            begin = self.u32() if self.version == 1 else self.u64()
            self.variables[name] = (datatype, [self.dims[i] for i in dims], begin)

    def u32(self):
        value = struct.unpack_from(">I", self.data, self.pos)[0]
        self.pos += 4
        return value

    def u64(self):
        value = struct.unpack_from(">Q", self.data, self.pos)[0]
        self.pos += 8
        return value

    def string(self):
        n = self.u32()
        result = self.data[self.pos:self.pos+n].decode("utf-8")
        self.pos += (n + 3) // 4 * 4
        return result

    def attributes(self):
        tag, count = self.u32(), self.u32()
        if tag not in (0, 12):
            raise ValueError("Invalid NetCDF attributes")
        for _ in range(count):
            self.string()
            datatype, n = self.u32(), self.u32()
            size = TYPE_SIZE[datatype] * n
            self.pos += (size + 3) // 4 * 4
            if self.pos > len(self.data):
                raise ValueError("NetCDF header exceeds range buffer")


def read_subset(read_range):
    data = read_range(0, 262143)
    header = Header(data)
    columns = []
    nrows = None
    for name in NAMES:
        datatype, shape, offset = header.variables[name]
        if datatype != 2 or len(shape) != 2:
            raise ValueError(f"Invalid trajectory string column: {name}")
        n, width = shape
        if nrows is None:
            nrows = n
        elif nrows != n:
            raise ValueError("Inconsistent subset string dimensions")
        size = n * width
        raw = data[offset:offset+size] if offset + size <= len(data) else read_range(offset, offset+size-1)
        if len(raw) != size:
            raise ValueError("Incomplete subset string range")
        values = np.frombuffer(raw, dtype=f"S{width}").tolist()
        columns.append([v.decode("utf-8").rstrip("\x00") for v in values])
    return [list(row) for row in zip(*columns)]


def refresh_cloud_subset(destination):
    """Return cached/current union over every currently published CF file."""
    cache_blob = destination.bucket.get_blob(destination.state_prefix + "/subset-cache.json")
    cache = json.loads(cache_blob.download_as_text()) if cache_blob else {}
    current = {}
    rows = set()
    scanned = reused = 0
    for blob in destination.bucket.list_blobs(prefix=destination.prefix + "/"):
        name = blob.name.rsplit("/", 1)[-1]
        if "/" in blob.name[len(destination.prefix)+1:] or not name.endswith(".nc"):
            continue
        generation = str(blob.generation)
        old = cache.get(name)
        if old and old["generation"] == generation:
            subset = old["rows"]
            reused += 1
        else:
            subset = read_subset(lambda start, end: blob.download_as_bytes(start=start, end=end, if_generation_match=int(generation)))
            scanned += 1
        current[name] = {"generation": generation, "rows": subset}
        rows.update(map(tuple, subset))
    if not current or not rows:
        raise ValueError("Empty published archive/subset; refusing subset replacement")
    destination.bucket.blob(destination.state_prefix + "/subset-cache.json").upload_from_string(json.dumps(current), content_type="application/json")
    payload = {"table": {"columnNames": list(NAMES), "columnTypes": ["String"]*5,
                         "columnUnits": [None]*5, "rows": sorted(rows)}}
    destination.bucket.blob(destination.state_prefix + "/subset/erdGtsppBest.json").upload_from_string(json.dumps(payload), content_type="application/json")
    return {"files_scanned": scanned, "files_reused": reused, "subset_rows": len(rows)}

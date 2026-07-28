import json
import time
from pathlib import Path


time.sleep(1.5)
request = dict(
    line.split("=", 1)
    for line in Path("in/request.txt").read_text(encoding="utf-8").splitlines()
)
Path("out").mkdir(exist_ok=True)
Path("out/result.json").write_text(
    json.dumps(
        {
            "request_id": request["request_id"],
            "status": "completed",
            "records": 3,
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
print("background report completed")

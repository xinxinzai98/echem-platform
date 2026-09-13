#!/usr/bin/env python3
"""Print a metadata-only retention plan; never delete or read raw BLOB content."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from echem_platform.start_stop_retention import artifact_retention_plan


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database",type=Path,required=True)
    parser.add_argument("--keep-render",type=int,default=3)
    parser.add_argument("--keep-scan",type=int,default=2)
    parser.add_argument("--keep-days",type=int,default=7)
    parser.add_argument("--pin",type=int,action="append",default=[])
    args=parser.parse_args(argv)
    path=args.database.expanduser().resolve()
    if not path.is_file():
        parser.error("数据库不存在。")
    connection=sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=15)
    connection.row_factory=sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        plan=artifact_retention_plan(connection,keep_render=args.keep_render,keep_scan=args.keep_scan,
                                     keep_days=args.keep_days,pinned_ids=args.pin)
        print(json.dumps(plan,ensure_ascii=False,indent=2))
    except (ValueError,sqlite3.Error) as exc:
        parser.error(str(exc))
    finally:
        connection.close()
    return 0


if __name__=="__main__":
    raise SystemExit(main())

"""Verify normal external MCP reads against fixtures prepared by the serving PID."""
import argparse
import json
from pathlib import Path
import httpx


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ember-env-file",type=Path,required=True)
    parser.add_argument("--process-report",type=Path,required=True)
    parser.add_argument("--result-file",type=Path,required=True)
    args=parser.parse_args()
    settings=dict(line.split("=",1) for line in args.ember_env_file.read_text(encoding="utf-8-sig").splitlines()
                  if line.strip() and not line.lstrip().startswith("#") and "=" in line)
    source=json.loads(args.process_report.read_text(encoding="utf-8-sig"))
    if source["status"]!="passed":
        raise RuntimeError("Startup source self-check has not passed")
    results=[]
    with httpx.Client(base_url=settings["EMBER_BASE_URL"],timeout=15,trust_env=False,
                      headers={"Authorization":"Bearer "+settings["EMBER_TOKEN"],
                               "Accept":"application/json, text/event-stream"}) as client:
        for number,fixture in enumerate(source["mcp_fixtures"],1):
            response=client.post("/mcp/",headers={"X-Heart-Instance":fixture["instance"]},json={
                "jsonrpc":"2.0","id":number,"method":"tools/call",
                "params":{"name":"read_memory_source","arguments":{"event_id":fixture["event_id"]}}})
            if response.status_code!=200:
                raise RuntimeError("MCP transport failed with status "+str(response.status_code))
            if "text/event-stream" in response.headers.get("content-type",""):
                envelope=next(json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith("data:"))
            else:
                envelope=response.json()
            result=json.loads(envelope["result"]["content"][0]["text"])
            ok=result.get("status")==fixture["expected_status"]
            if fixture["expected_status"]=="ok":
                ok=ok and result.get("transcript")==fixture["expected_transcript"]
            else:
                ok=ok and "transcript" not in result
            results.append({"name":fixture["name"],"instance":fixture["instance"],"status":result.get("status"),"passed":ok})
    report={"status":"passed" if all(row["passed"] for row in results) else "failed",
            "serving_pid_from_startup_report":source["pid"],"address":settings["EMBER_BASE_URL"],
            "execution":"external HTTP/MCP only; no direct Ember object/client invocation in probe driver",
            "cases":results,"credentials_disclosed":False,"production_verified":False}
    args.result_file.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"status":report["status"],"pid":source["pid"],"mcp_cases":len(results),"passed":sum(row["passed"] for row in results)}))
    if report["status"]!="passed":
        raise RuntimeError("External MCP source validation failed")

if __name__=="__main__":
    main()

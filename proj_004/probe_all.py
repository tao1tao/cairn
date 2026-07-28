#!/usr/bin/env python3
"""Phase 1: Probe all Huawei Cloud hosts on ports 18100/19098/19099/29098"""
import subprocess, json, sys, hashlib, ssl, socket, time
from concurrent.futures import ThreadPoolExecutor, as_completed

HOSTS = {
    "SG":  ["124.243.150.232", "119.13.105.129"],
    "MX":  ["122.8.178.14", "122.8.181.116", "110.238.85.188", "122.8.181.108", "101.44.185.91"],
    "SA":  ["101.46.59.254", "101.46.59.62"],
}
PORTS = [18100, 19098, 19099, 29098]

def probe(host, port, is_ssl):
    scheme = "https" if is_ssl else "http"
    url = f"{scheme}://{host}:{port}/"
    result = {"host": host, "port": port, "scheme": scheme, "error": None, "status": None, "body_len": None, "headers": {}, "body_md5": None, "server": None}

    try:
        r = subprocess.run(
            ["curl", "-s", "-S", "-i", "--max-time", "8", "-k", "-H", "User-Agent: Mozilla/5.0", url],
            capture_output=True, text=True, timeout=10
        )
        output = r.stdout + r.stderr
        # Parse response
        lines = output.split("\n")
        # HTTP status
        for line in lines:
            if line.startswith("HTTP/"):
                parts = line.split()
                if len(parts) >= 2:
                    result["status"] = int(parts[1])
                break

        # Headers
        body_start = 0
        in_headers = True
        for i, line in enumerate(lines):
            if in_headers and line.strip() == "":
                body_start = i + 1
                in_headers = False
                continue
            if in_headers:
                if line.startswith("Server:") or line.startswith("server:"):
                    result["server"] = line.split(":", 1)[1].strip()
                if ":" in line:
                    k, v = line.split(":", 1)
                    result["headers"][k.strip().lower()] = v.strip()

        body = "\n".join(lines[body_start:])
        result["body_len"] = len(body)
        if body:
            result["body_md5"] = hashlib.md5(body.encode()).hexdigest()
            # Truncated body preview for analysis
            preview = body[:300].strip()
            result["body_preview"] = preview

        # SSL cert info if applicable
        if is_ssl and not result["error"]:
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                with socket.create_connection((host, port), timeout=8) as sock:
                    with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                        cert = ssock.getpeercert()
                        if cert:
                            result["ssl_cn"] = dict(cert.get("subject", [])).get("commonName", "N/A")
                            result["ssl_issuer"] = cert.get("issuer", [])
                            result["ssl_expiry"] = cert.get("notAfter", "N/A")
            except Exception as e:
                result["ssl_error"] = str(e)

    except subprocess.TimeoutExpired:
        result["error"] = "TIMEOUT"
    except Exception as e:
        result["error"] = str(e)

    return result

def get_tls_fingerprint(host, port):
    """Get TLS certificate information and supported versions"""
    results = {}
    for ver_name, method in [("TLSv1.0", ssl.PROTOCOL_TLSv1),
                              ("TLSv1.1", ssl.PROTOCOL_TLSv1_1),
                              ("TLSv1.2", ssl.PROTOCOL_TLSv1_2),
                              ("TLSv1.3", ssl.PROTOCOL_TLS)]:
        try:
            ctx = ssl.SSLContext(method)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with socket.create_connection((host, port), timeout=8) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                    results[ver_name] = ssock.version()
        except Exception:
            results[ver_name] = "FAILED"
    return results

# Probe all combinations
tasks = []
is_ssl_ports = {18100, 19098}  # these were HTTPS per f001
is_http_ports = {19099, 29098}  # these were HTTP per f001

for region, hosts in HOSTS.items():
    for host in hosts:
        for port in PORTS:
            is_ssl = port in is_ssl_ports
            tasks.append((host, port, is_ssl, region))

print(f"Total probes: {len(tasks)}", flush=True)

results = []
with ThreadPoolExecutor(max_workers=20) as executor:
    futures = {executor.submit(probe, h, p, s): (h, p, r) for h, p, s, r in tasks}
    for fut in as_completed(futures):
        r = fut.result()
        results.append(r)
        h, p = r["host"], r["port"]
        status = r["status"] if not r["error"] else f"ERROR:{r['error']}"
        body_len = r.get("body_len", -1)
        server = r.get("server", "N/A") or "N/A"
        print(f"  {h}:{p} -> status={status} len={body_len} server={server}", flush=True)

# For SSL ports that respond, get TLS fingerprints
print("\n=== TLS Certificate Details (SSL-enabled ports) ===", flush=True)
tls_results = []
for region, hosts in HOSTS.items():
    for host in hosts:
        for port in [18100, 19098]:
            # Only probe if it responded to basic probe
            matching = [r for r in results if r["host"] == host and r["port"] == port and not r["error"]]
            if matching and matching[0]["status"] and matching[0]["status"] < 500:
                tls = get_tls_fingerprint(host, port)
                tls_entry = {"host": host, "port": port, "tls": tls}
                tls_results.append(tls_entry)
                print(f"  {host}:{port} TLS: {json.dumps(tls)}", flush=True)

# Save full results
with open("service_probe_results.json", "w") as f:
    json.dump({"probes": results, "tls": tls_results}, f, indent=2, default=str)

# Summary by region
print("\n=== REGIONAL SUMMARY ===", flush=True)
for region, hosts in HOSTS.items():
    print(f"\n--- {region} ---", flush=True)
    for host in hosts:
        host_results = [r for r in results if r["host"] == host]
        for r in host_results:
            s = r["status"] if not r["error"] else "ERR"
            print(f"  {r['host']}:{r['port']} ({r['scheme']}) -> {s} | len={r.get('body_len','?')} | svr={r.get('server','?')}", flush=True)

print("\nDone. Full results in service_probe_results.json", flush=True)

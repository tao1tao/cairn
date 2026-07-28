#!/usr/bin/env python3
"""Phase 2: Deep probe of responsive hosts — path discovery, Host header tests, API probing"""
import subprocess, json, hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed

HOSTS = {
    "SG":  ["124.243.150.232", "119.13.105.129"],
    "MX":  ["122.8.178.14", "122.8.181.116", "110.238.85.188", "122.8.181.108"],
    "SA":  ["101.46.59.254", "101.46.59.62"],
}
# 101.44.185.91 excluded - all ports unreachable

# Interesting paths to probe on each service
PROBE_PATHS_ADMIN = [
    "/", "/api", "/api/v1", "/api/v2", "/swagger", "/swagger-ui", "/swagger-ui.html",
    "/api-docs", "/openapi.json", "/v1", "/v2", "/health", "/healthz",
    "/status", "/info", "/actuator", "/actuator/info", "/actuator/health",
    "/metrics", "/prometheus", "/version", "/ping", "/test", "/debug",
    "/admin", "/manage", "/management", "/console", "/login", "/graphql",
    "/.env", "/.git/config", "/robots.txt", "/sitemap.xml", "/favicon.ico",
    "/nginx_status", "/server-status", "/.well-known/", "/crossdomain.xml",
    "/clientaccesspolicy.xml", "/ws", "/websocket", "/socket.io",
]

PROBE_PATHS_API_AUTH = [
    "/api/user", "/api/users", "/api/login", "/api/auth", "/api/token",
    "/api/admin", "/api/health", "/api/config", "/api/setting", "/api/settings",
    "/api/data", "/api/v1/data", "/api/v1/user", "/api/v1/admin",
    "/api/v1/health", "/api/v1/config", "/api/v1/status",
    "/api/private", "/api/internal", "/api/v1/private", "/api/v1/internal",
    "/graphql", "/api/graphql", "/v1/graphql",
]

PROBE_PATHS_TRAVERSAL = [
    "/../../../etc/passwd", "/..%252f..%252f..%252fetc/passwd",
    "/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
    "/static/../../../etc/passwd",
    "/api/../../../etc/passwd",
]

def check_https_on_29098(host):
    """Test if 29098 accepts HTTPS"""
    results = {}
    for schema in ["https", "http"]:
        try:
            r = subprocess.run(
                ["curl", "-s", "-S", "-D-", "--max-time", "8", "-k",
                 "-H", "User-Agent: Mozilla/5.0",
                 f"{schema}://{host}:29098/"],
                capture_output=True, text=True, timeout=10
            )
            out = r.stdout + r.stderr
            lines = out.split("\n")
            status = None
            for line in lines:
                if line.startswith("HTTP/"):
                    parts = line.split()
                    if len(parts) >= 2:
                        status = int(parts[1])
                    break
            results[schema] = status
        except:
            results[schema] = "ERROR"
    return results

def probe_path(host, port, path, is_ssl):
    scheme = "https" if is_ssl else "http"
    url = f"{scheme}://{host}:{port}{path}"
    try:
        r = subprocess.run(
            ["curl", "-s", "-S", "-o", "/dev/null", "-w", "%{http_code}:%{size_download}:%{content_type}",
             "--max-time", "8", "-k", "-H", "User-Agent: Mozilla/5.0", url],
            capture_output=True, text=True, timeout=10
        )
        stdout = r.stdout.strip()
        stderr = r.stderr.strip()
        return {"host": host, "port": port, "path": path, "result": stdout, "error": stderr if stderr else None}
    except Exception as e:
        return {"host": host, "port": port, "path": path, "result": "TIMEOUT", "error": str(e)}

def check_host_header_routing(host, port, is_ssl):
    """Test different Host headers to see if virtual hosting reveals different apps"""
    scheme = "https" if is_ssl else "http"
    test_hosts = [
        host,  # default
        "cu-sc.com",
        "www.cu-sc.com",
        "api.cu-sc.com",
        "vpn.cu-sc.com",
        "monitor.cu-sc.com",
        "internal.cu-sc.com",
        "admin.cu-sc.com",
        "test.cu-sc.com",
        "dev.cu-sc.com",
        "cloud.cu-sc.com",
        "backend.cu-sc.com",
    ]
    results = []
    for hdr in test_hosts:
        try:
            r = subprocess.run(
                ["curl", "-s", "-S", "-D-", "--max-time", "8", "-k",
                 "-H", f"Host: {hdr}",
                 "-H", "User-Agent: Mozilla/5.0",
                 f"{scheme}://{host}:{port}/"],
                capture_output=True, text=True, timeout=10
            )
            out = r.stdout + r.stderr
            lines = out.split("\n")
            status = None
            body_len = 0
            server = "N/A"
            body_start = 0
            in_header = True
            for i, line in enumerate(lines):
                if line.startswith("HTTP/"):
                    parts = line.split()
                    if len(parts) >= 2:
                        status = int(parts[1])
                if in_header and line.strip() == "":
                    body_start = i + 1
                    in_header = False
                if in_header and ":" in line:
                    k, v = line.split(":", 1)
                    if k.strip().lower() == "server":
                        server = v.strip()
            body = "\n".join(lines[body_start:])
            body_len = len(body)
            md5 = hashlib.md5(body.encode()).hexdigest()
            results.append({"host_header": hdr, "status": status, "body_len": body_len, "body_md5": md5, "server": server})
        except Exception as e:
            results.append({"host_header": hdr, "error": str(e)})
    return {"target_host": host, "port": port, "results": results}

def check_methods(host, port, is_ssl):
    """Test HTTP methods"""
    scheme = "https" if is_ssl else "http"
    url = f"{scheme}://{host}:{port}/"
    methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD", "TRACE", "CONNECT"]
    results = {}
    for method in methods:
        try:
            r = subprocess.run(
                ["curl", "-s", "-S", "-o", "/dev/null", "-w", "%{http_code}", "-X", method,
                 "--max-time", "8", "-k", "-H", "User-Agent: Mozilla/5.0", url],
                capture_output=True, text=True, timeout=10
            )
            results[method] = r.stdout.strip()
        except:
            results[method] = "TIMEOUT"
    return {"host": host, "port": port, "methods": results}

def check_ssl_cert_details(host, port):
    """Get SSL certificate details"""
    try:
        r = subprocess.run(
            ["openssl", "s_client", "-connect", f"{host}:{port}",
             "-servername", host, "-tlsextdebug", "</dev/null", "2>/dev/null"],
            capture_output=True, text=True, timeout=10
        )
        out = r.stdout
        cert_info = {}
        for line in out.split("\n"):
            if "subject=" in line:
                cert_info["subject"] = line
            if "issuer=" in line:
                cert_info["issuer"] = line
            if "notBefore=" in line:
                cert_info["notBefore"] = line
            if "notAfter=" in line:
                cert_info["notAfter"] = line
        # Extract SAN
        if "X509v3 Subject Alternative Name" in out:
            idx = out.index("X509v3 Subject Alternative Name")
            san_line = out[idx:idx+500].split("\n")[0]
            cert_info["san"] = san_line
        return cert_info
    except Exception as e:
        return {"error": str(e)}

def compare_body_content(host, port, is_ssl):
    """Get full body for content comparison across regions"""
    scheme = "https" if is_ssl else "http"
    url = f"{scheme}://{host}:{port}/"
    try:
        r = subprocess.run(
            ["curl", "-s", "-S", "-D-", "--max-time", "8", "-k",
             "-H", "User-Agent: Mozilla/5.0", url],
            capture_output=True, text=True, timeout=10
        )
        out = r.stdout + r.stderr
        lines = out.split("\n")
        body_start = 0
        in_header = True
        headers = {}
        for i, line in enumerate(lines):
            if in_header and line.strip() == "":
                body_start = i + 1
                in_header = False
            if in_header and ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        body = "\n".join(lines[body_start:])
        return {"headers": headers, "body_md5": hashlib.md5(body.encode()).hexdigest(), "body_len": len(body), "body": body}
    except Exception as e:
        return {"error": str(e)}

# Which SSL config for each port
PORT_SSL = {18100: True, 19098: True, 19099: False, 29098: True}  # 29098 expects HTTPS

# --- Run tests ---
all_responsive = [
    ("SG", "124.243.150.232", 18100),
    ("SG", "124.243.150.232", 19098),
    ("SG", "119.13.105.129", 18100),
    ("SG", "119.13.105.129", 19098),
    ("SG", "119.13.105.129", 19099),
    ("MX", "122.8.178.14", 18100),
    ("MX", "122.8.178.14", 19098),
    ("MX", "122.8.181.116", 18100),
    ("MX", "122.8.181.116", 19098),
    ("MX", "110.238.85.188", 18100),
    ("MX", "110.238.85.188", 19098),
    ("MX", "110.238.85.188", 19099),
    ("MX", "110.238.85.188", 29098),
    ("MX", "122.8.181.108", 18100),
    ("MX", "122.8.181.108", 19098),
    ("MX", "122.8.181.108", 19099),
    ("MX", "122.8.181.108", 29098),
    ("SA", "101.46.59.254", 19098),
    ("SA", "101.46.59.254", 19099),
    ("SA", "101.46.59.62", 18100),
    ("SA", "101.46.59.62", 19098),
]

# 1. HTTPS on 29098 test
print("### Test 1: HTTPS on port 29098 ###", flush=True)
for region, hosts in [("SG", ["124.243.150.232"]), ("MX", ["110.238.85.188", "122.8.181.108"])]:
    for host in hosts:
        res = check_https_on_29098(host)
        print(f"  {host}:29098 -> {res}", flush=True)

# 2. SSL Certificate details per host
print("\n### Test 2: SSL Certificate Details (port 18100) ###", flush=True)
for region, host, port in all_responsive:
    if port == 18100:
        cert = check_ssl_cert_details(host, port)
        print(f"  {host}:{port} -> {json.dumps(cert.get('subject','N/A'))}", flush=True)
        if "san" in cert:
            print(f"    SAN: {cert['san']}", flush=True)

# 3. HTTP Methods test on a sample from each region/port
print("\n### Test 3: HTTP Methods (sample per region per port) ###", flush=True)
tested = set()
for region, host, port in all_responsive:
    key = f"{region}_{port}"
    if key not in tested:
        tested.add(key)
        res = check_methods(host, port, PORT_SSL[port])
        print(f"  {host}:{port} -> {json.dumps(res['methods'])}", flush=True)

# 4. Path probing on port 18100 and 19098
print("\n### Test 4: Endpoint probe on responsive services ###", flush=True)
probe_targets = []
for region, host, port in all_responsive:
    if port in [18100, 19098, 19099]:
        probe_targets.append((host, port, PORT_SSL[port]))

# Sample a subset across regions for path probing
sampled = {}
for host, port, is_ssl in probe_targets:
    key = f"{port}"
    if key not in sampled:
        sampled[key] = []
    if len(sampled[key]) < 3:  # max 3 hosts per port
        sampled[key].append((host, port, is_ssl))

path_tasks = []
for port_key, hosts in sampled.items():
    for host, port, is_ssl in hosts:
        for path in PROBE_PATHS_ADMIN:
            path_tasks.append((host, port, path, is_ssl))

print(f"  Probing {len(path_tasks)} path/endpoint combinations...", flush=True)
path_results = []
with ThreadPoolExecutor(max_workers=30) as ex:
    futs = {ex.submit(probe_path, h, p, pa, s): (h, p, pa) for h, p, pa, s in path_tasks}
    for fut in as_completed(futs):
        r = fut.result()
        path_results.append(r)
        if r["result"] and not r["result"].startswith("4") and not r["result"].startswith("3"):
            # Non-4xx/3xx responses are interesting
            print(f"  [!!] {r['host']}:{r['port']}{r['path']} -> {r['result']}", flush=True)

# 5. Host header routing tests
print("\n### Test 5: Host Header Virtual Host Routing ###", flush=True)
host_header_tasks = []
# Pick one from each region × port combination
hh_targets = [
    ("124.243.150.232", 18100), ("124.243.150.232", 19098),
    ("119.13.105.129", 18100), ("119.13.105.129", 19098),
    ("122.8.178.14", 18100),
    ("110.238.85.188", 18100), ("110.238.85.188", 19098),
    ("101.46.59.62", 18100), ("101.46.59.254", 19098),
]
hh_results = []
for host, port in hh_targets:
    res = check_host_header_routing(host, port, PORT_SSL[port])
    hh_results.append(res)
    # Show only interesting findings
    for r in res["results"]:
        if r.get("status") and r["status"] not in [403, 404]:
            print(f"  [!!] {host}:{port} Host:{r['host_header']} -> {r['status']} len={r.get('body_len')} md5={r.get('body_md5','')}", flush=True)

# Save all results
output = {
    "path_probes": path_results,
    "host_header": hh_results,
}
with open("service_probe_phase2.json", "w") as f:
    json.dump(output, f, indent=2, default=str)

print("\nPhase 2 complete. Results saved to service_probe_phase2.json", flush=True)

#!/usr/bin/env python3
"""camapi.py — Sony Camera Remote API (ScalarWebAPI v1) discovery + test tool.

Runs standalone on the Pi (stdlib only). Point the Pi's Wi-Fi at the camera's
network first, then:

    sudo python3 camapi.py discover        # SSDP search for the camera's API
    python3 camapi.py probe <base-url>     # probe a known endpoint URL
    python3 camapi.py apis                 # getAvailableApiList
    python3 camapi.py zoom in start [speed]  # actZoom
    python3 camapi.py zoom out stop
    python3 camapi.py rec start            # startMovieRec
    python3 camapi.py rec stop             # stopMovieRec
    python3 camapi.py watch                # poll status/event every second

The first working base URL is cached in ./camapi_endpoint.txt.
"""
import json
import socket
import sys
import time
import urllib.request

SSDP_ADDR = ('239.255.255.250', 1900)
SEARCH_TARGETS = [
    'urn:schemas-sony-com:service:ScalarWebAPI:1',
    'urn:schemas-sony-com:service:CameraRemoteAPI:1',
    'upnp:rootdevice',
]
ST = 5.0

# ---------------------------------------------------------------- http ----

def http_get(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode('utf-8', 'replace'), r.status
    except Exception as e:
        return str(e), 0

def api_call(base, method, params=None, version='1.0', timeout=8):
    """POST JSON-RPC to the camera API. base = endpoint URL."""
    body = json.dumps({'method': method,
                       'params': params or [],
                       'id': 1,
                       'version': version}).encode()
    req = urllib.request.Request(base, data=body,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode('utf-8', 'replace'))
    except Exception as e:
        return 0, {'error': str(e)}

# ------------------------------------------------------------ discovery ---

def ssdp_discover():
    found = {}
    for target in SEARCH_TARGETS:
        msg = (f'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n'
               f'MAN: "ssdp:discover"\r\nMX: 2\r\nST: {target}\r\n\r\n')
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.settimeout(ST)
            s.sendto(msg.encode(), SSDP_ADDR)
            deadline = time.time() + ST
            while time.time() < deadline:
                try:
                    data, addr = s.recvfrom(4096)
                    text = data.decode('utf-8', 'replace')
                    headers = {}
                    for line in text.splitlines():
                        if ':' in line:
                            k, v = line.split(':', 1)
                            headers[k.strip().upper()] = v.strip()
                    loc = headers.get('LOCATION')
                    if loc:
                        found[addr[0]] = {'location': loc, 'st': headers.get('ST')}
                except socket.timeout:
                    break
        finally:
            s.close()
    return found

def extract_endpoint(desc_url):
    """Parse the device description XML for X_ScalarWebAPI endpoints."""
    text, _ = http_get(desc_url, timeout=5)
    if not text:
        return []
    import re
    eps = []
    # <X_ScalarWebAPI_EndpointURL>http://...</X_ScalarWebAPI_EndpointURL>
    for m in re.finditer(r'<X_ScalarWebAPI_EndpointURL>([^<]+)</X_ScalarWebAPI_EndpointURL>', text):
        eps.append(('scalar', m.group(1).strip()))
    # older style: <X_ScalarWebAPI_ServiceType>camera...</> with base url separate
    base = re.search(r'<URLBase>([^<]+)</URLBase>', text)
    for m in re.finditer(r'<X_ScalarWebAPI_ServiceType>([^<]+)</X_ScalarWebAPI_ServiceType>', text):
        svc = m.group(1).strip()
        if base:
            u = base.group(1).strip()
            if not u.endswith('/'):
                u += '/'
            eps.append(('service', u + svc))
    return eps

def save_endpoint(url):
    with open('camapi_endpoint.txt', 'w') as f:
        f.write(url)
    print('saved endpoint:', url)

def load_endpoint():
    try:
        return open('camapi_endpoint.txt').read().strip()
    except FileNotFoundError:
        return None

def cmd_discover():
    print('SSDP search (5s per target)...')
    hits = ssdp_discover()
    if not hits:
        print('NO SSDP RESPONSES. Check: Pi on the camera Wi-Fi? multicast enabled?')
        print('Fallback: try the camera\'s IP directly with "probe http://<ip>:10000/"')
        return
    endpoints = []
    for ip, info in sorted(hits.items()):
        print(f'\n{ip}  ST={info["st"]}')
        print(f'  LOCATION: {info["location"]}')
        for kind, ep in extract_endpoint(info['location']):
            print(f'  {kind} endpoint: {ep}')
            endpoints.append(ep)
    if not endpoints:
        print('\nNo X_ScalarWebAPI endpoint in the description XML(s). Try probe on guessed URLs.')
        return
    print('\nProbing endpoints...')
    for ep in endpoints:
        st, resp = api_call(ep, 'getAvailableApiList')
        print(f'{ep} -> HTTP {st}: {json.dumps(resp)[:300]}')
        if st == 200:
            save_endpoint(ep)

def cmd_probe(url):
    if not url.endswith('/'):
        url += '/'
    cands = [url, url + 'camera', url + 'sony/camera']
    for c in cands:
        st, resp = api_call(c, 'getAvailableApiList')
        print(f'{c} -> HTTP {st}: {json.dumps(resp)[:400]}')
        if st == 200:
            save_endpoint(c)
            return
    print('None of the probed URLs answered. Full JSON-RPC required.')

def ensure_endpoint(url=None):
    base = url or load_endpoint()
    if not base:
        sys.exit('No endpoint cached. Run: sudo python3 camapi.py discover  '
                 'or: python3 camapi.py probe http://<ip>:10000/')
    return base

def cmd_apis(base):
    st, resp = api_call(base, 'getAvailableApiList')
    print(f'HTTP {st}')
    print(json.dumps(resp, indent=2))

def cmd_zoom(base, direction, state, speed=None):
    # 2014-era firmware (MC2500): exactly 2 params. Speed param only on newer fw.
    params = [direction, state]
    if speed:
        params = [direction, state, speed]
    st, resp = api_call(base, 'actZoom', params, version='1.0')
    print(f'HTTP {st}: {json.dumps(resp)}')

def cmd_rec(base, action):
    st, resp = api_call(base, 'startMovieRec' if action == 'start' else 'stopMovieRec')
    print(f'HTTP {st}: {json.dumps(resp)}')

def cmd_watch(base, n=30):
    for i in range(n):
        st, resp = api_call(base, 'getEvent', [False], version='1.0', timeout=4)
        ev = {}
        if isinstance(resp, dict):
            res = resp.get('result')
            if isinstance(res, list) and res and isinstance(res[0], dict):
                ev = res[0]
        zoom = ev.get('zoomInformation')
        rec = ev.get('cameraFunction') or ev.get('movieRecording')
        print(f'{i:02d} HTTP {st} zoom={json.dumps(zoom)} rec={json.dumps(rec)}')
        time.sleep(1)

# ----------------------------------------------------------------- main ---

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == 'discover':
        cmd_discover()
    elif cmd == 'probe':
        cmd_probe(sys.argv[2])
    else:
        # commands that need the endpoint: first arg after cmd may BE the
        # endpoint (http...), otherwise load the cached one
        rest = sys.argv[2:]
        base = None
        if rest and rest[0].startswith('http'):
            base = rest.pop(0)
        base = ensure_endpoint(base)
        if cmd == 'apis':
            cmd_apis(base)
        elif cmd == 'zoom':
            cmd_zoom(base, rest[0], rest[1], rest[2] if len(rest) > 2 else None)
        elif cmd == 'rec':
            cmd_rec(base, rest[0])
        elif cmd == 'watch':
            cmd_watch(base)
        else:
            print(__doc__)

if __name__ == '__main__':
    main()

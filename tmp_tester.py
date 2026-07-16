import urllib.request, json

def test_url(url):
    print(f"Testing {url}...")
    try:
        data = json.dumps({"store_id": "test_store_id"}).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req) as response:
            print(f"Status: {response.getcode()}")
            print(f"Body: {response.read().decode()}")
    except urllib.error.HTTPError as e:
        print(f"HTTP Error: {e.code} - {e.reason}")
        print(f"Error body: {e.read().decode()}")
    except Exception as e:
        print(f"General Error: {e}")

test_url("http://localhost:3000/api/session")
test_url("http://localhost:3000/api/session/")


import requests
import json

APP_ID = "b4f45c80-9a75-4d4f-b16a-1b84e0c79ed4"
API_TOKEN = "dop_v1_153558ff94f1f7f6be775692dba269d755b8cac87042bda4c23d75717fce490d"

def inspect_spec():
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json"
    }
    url = f"https://api.digitalocean.com/v2/apps/{APP_ID}"
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            app_data = response.json()
            print(json.dumps(app_data['app']['spec'], indent=2))
        else:
            print(f"Error: {response.status_code}")
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    inspect_spec()

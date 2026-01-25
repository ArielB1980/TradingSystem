
import requests
import json

API_TOKEN = "dop_v1_153558ff94f1f7f6be775692dba269d755b8cac87042bda4c23d75717fce490d"

def list_apps():
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json"
    }
    url = "https://api.digitalocean.com/v2/apps"
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            apps = response.json().get('apps', [])
            print(f"Found {len(apps)} apps:")
            for app in apps:
                print(f"- Name: {app['spec']['name']}, ID: {app['id']}")
        else:
            print(f"Error listing apps: {response.status_code}")
            print(response.text)
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    list_apps()

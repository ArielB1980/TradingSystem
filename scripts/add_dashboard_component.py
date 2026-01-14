#!/usr/bin/env python3
"""
Add dashboard component to DigitalOcean App Platform via API.

Requires DigitalOcean API token with write access.

Usage:
    export DIGITALOCEAN_API_TOKEN=your_token
    python scripts/add_dashboard_component.py
"""
import os
import sys
import requests
import json

BASE_URL = "https://api.digitalocean.com/v2"
APP_NAME = "tradingbot-2tdzi"

def get_api_token():
    """Get API token from environment."""
    token = os.getenv("DIGITALOCEAN_API_TOKEN")
    if not token:
        print("❌ Error: DIGITALOCEAN_API_TOKEN environment variable not set")
        print("\nTo get your API token:")
        print("1. Go to https://cloud.digitalocean.com/account/api/tokens")
        print("2. Generate a new token with write access")
        print("3. Run: export DIGITALOCEAN_API_TOKEN=your_token")
        sys.exit(1)
    return token

def get_app_id(token):
    """Get app ID by name."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    response = requests.get(f"{BASE_URL}/apps", headers=headers)
    if response.status_code != 200:
        print(f"❌ Failed to get apps: {response.status_code}")
        print(response.text)
        return None
    
    apps = response.json().get("apps", [])
    for app in apps:
        if APP_NAME in app.get("spec", {}).get("name", "").lower():
            return app.get("id")
    
    return None

def get_app_spec(token, app_id):
    """Get current app spec."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    response = requests.get(f"{BASE_URL}/apps/{app_id}", headers=headers)
    if response.status_code != 200:
        print(f"❌ Failed to get app: {response.status_code}")
        return None
    
    return response.json().get("app", {}).get("spec", {})

def add_dashboard_component(token, app_id, spec):
    """Add dashboard component to app spec."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    # Check if dashboard already exists
    services = spec.get("services", [])
    for service in services:
        if service.get("name") == "dashboard":
            print("⚠️  Dashboard component already exists")
            return False
    
    # Add dashboard service
    dashboard_service = {
        "name": "dashboard",
        "github": {
            "repo": "ArielB1980/TradingSystem",
            "branch": "main"
        },
        "run_command": "streamlit run src/dashboard/streamlit_app.py --server.port 8080 --server.address 0.0.0.0 --server.headless true",
        "http_port": 8080,
        "instance_count": 1,
        "instance_size_slug": "basic-xxs",
        "routes": [
            {
                "path": "/dashboard"
            }
        ],
        "envs": [
            {
                "key": "DATABASE_URL",
                "scope": "RUN_TIME",
                "type": "SECRET"
            },
            {
                "key": "ENVIRONMENT",
                "value": "prod",
                "scope": "RUN_TIME",
                "type": "GENERAL"
            }
        ]
    }
    
    services.append(dashboard_service)
    spec["services"] = services
    
    # Update app
    update_data = {
        "spec": spec
    }
    
    response = requests.put(f"{BASE_URL}/apps/{app_id}", 
                          headers=headers, 
                          json=update_data)
    
    if response.status_code in [200, 201]:
        print("✅ Dashboard component added successfully!")
        print("\n⏳ App Platform will deploy the dashboard component...")
        print("   Check status at: https://cloud.digitalocean.com/apps")
        return True
    else:
        print(f"❌ Failed to update app: {response.status_code}")
        print(response.text)
        return False

def main():
    print("=" * 60)
    print("Add Dashboard Component to App Platform")
    print("=" * 60)
    print()
    
    token = get_api_token()
    print("✅ API token found")
    
    print("\n📋 Finding app...")
    app_id = get_app_id(token)
    if not app_id:
        print("❌ App not found")
        return
    
    print(f"✅ Found app ID: {app_id}")
    
    print("\n📋 Getting app spec...")
    spec = get_app_spec(token, app_id)
    if not spec:
        print("❌ Failed to get app spec")
        return
    
    print("✅ Got app spec")
    
    print("\n➕ Adding dashboard component...")
    success = add_dashboard_component(token, app_id, spec)
    
    if success:
        print("\n🎉 Dashboard will be available after deployment!")
        print("   URL: Check App Platform for dashboard component URL")
    else:
        print("\n❌ Failed to add dashboard component")

if __name__ == "__main__":
    main()

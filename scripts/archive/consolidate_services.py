"""
Consolidate web and dashboard services into worker service on DigitalOcean App Platform.

This removes the 'web' and 'dashboard' services and makes 'worker' a web service
that handles all HTTP traffic (health checks, API endpoints, and dashboard).

Usage:
  export DO_API_TOKEN=your_token
  python scripts/consolidate_services.py
"""
import os
import sys
import requests
import json

BASE_URL = "https://api.digitalocean.com/v2"
APP_NAME = "tradingsystem"


def _token() -> str:
    t = os.environ.get("DO_API_TOKEN") or os.environ.get("DIGITALOCEAN_API_TOKEN")
    if not t:
        print("Set DO_API_TOKEN or DIGITALOCEAN_API_TOKEN")
        sys.exit(1)
    return t


def _headers() -> dict:
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}


def get_app_id():
    """Find the app ID by name."""
    print(f"Finding App ID for '{APP_NAME}'...")
    response = requests.get(f"{BASE_URL}/apps", headers=_headers())
    if response.status_code != 200:
        print(f"Error fetching apps: {response.text}")
        sys.exit(1)
        
    apps = response.json().get("apps", [])
    for app in apps:
        if app['spec']['name'] == APP_NAME:
            return app['id']
            
    # If not found by exact name, try to find the one we deployed to recently
    if apps:
        print(f"App '{APP_NAME}' not found. Available apps:")
        for a in apps:
            print(f"  - {a.get('spec', {}).get('name', 'Unknown')} (ID: {a.get('id', 'Unknown')})")
        if len(apps) == 1:
            print(f"Using: {apps[0]['spec']['name']}")
            return apps[0]['id']
        
    print("No apps found.")
    sys.exit(1)


def consolidate_services(app_id):
    """Consolidate web and dashboard into worker service."""
    print(f"Fetching App Spec for {app_id}...")
    response = requests.get(f"{BASE_URL}/apps/{app_id}", headers=_headers())
    if response.status_code != 200:
        print(f"Error fetching app: {response.text}")
        sys.exit(1)
        
    app_data = response.json()
    spec = app_data.get('app', {}).get('spec', {})
    
    services = spec.get('services', [])
    workers = spec.get('workers', [])
    
    print(f"\nCurrent configuration:")
    print(f"  Services: {[s['name'] for s in services]}")
    print(f"  Workers: {[w['name'] for w in workers]}")
    
    # Find worker service (could be in services or workers)
    worker = None
    for s in services:
        if s['name'] == 'worker':
            worker = s
            break
    
    if not worker:
        for w in workers:
            if w['name'] == 'worker':
                worker = w
                break
    
    if not worker:
        print("❌ Worker service not found!")
        sys.exit(1)
    
    # Find web and dashboard services to remove
    web_service = None
    dashboard_service = None
    
    for s in services:
        if s['name'] == 'web':
            web_service = s
        elif s['name'] == 'dashboard':
            dashboard_service = s
    
    if not web_service and not dashboard_service:
        print("✅ No web or dashboard services found. Already consolidated!")
        return False
    
    print(f"\n📋 Services to remove:")
    if web_service:
        print(f"  - web (costs $5/month)")
    if dashboard_service:
        print(f"  - dashboard (costs $5/month)")
    
    print(f"\n📋 Updating worker service:")
    print(f"  - Adding http_port: 8080")
    print(f"  - Adding routes: /, /dashboard")
    print(f"  - Worker will serve health API and Streamlit dashboard")
    
    # Remove web and dashboard from services
    spec['services'] = [s for s in services if s['name'] not in ['web', 'dashboard']]
    
    # Update worker service to have HTTP port and routes
    worker['http_port'] = 8080
    worker['routes'] = [
        {"path": "/"},
        {"path": "/dashboard"}
    ]
    
    # Remove ingress if it exists (component routes are mutually exclusive with ingress)
    if 'ingress' in spec:
        print(f"  - Removing app-level ingress (using component routes instead)")
        del spec['ingress']
    
    # Ensure worker is in services list (update existing or add)
    if 'services' not in spec:
        spec['services'] = []
    
    # Check if worker already exists as a service
    worker_exists = False
    for s in spec['services']:
        if s['name'] == 'worker':
            # Update existing
            s.update(worker)
            worker_exists = True
            break
    
    if not worker_exists:
        spec['services'].append(worker)
    
    # Remove worker from workers list if it was there
    if 'workers' in spec:
        spec['workers'] = [w for w in workers if w['name'] != 'worker']
    
    print(f"\n✅ Updated configuration:")
    print(f"  Services: {[s['name'] for s in spec.get('services', [])]}")
    print(f"  Workers: {[w['name'] for w in spec.get('workers', [])]}")
    
    print(f"\n💰 Cost savings: ${(len([s for s in [web_service, dashboard_service] if s]) * 5)}/month")
    
    print("\n⚠️  This will update your app and trigger a deployment.")
    confirm = input("Continue? (yes/no): ")
    if confirm.lower() != "yes":
        print("❌ Cancelled")
        return False
    
    print("\n🔄 Submitting updated App Spec to DigitalOcean...")
    update_response = requests.put(
        f"{BASE_URL}/apps/{app_id}",
        headers=_headers(),
        json={"spec": spec}
    )
    
    if update_response.status_code == 200:
        deployment = update_response.json().get('deployment', {})
        print(f"✅ Update successful! Deployment ID: {deployment.get('id')}")
        print("\n📝 Summary:")
        print(f"  - Removed 'web' service")
        print(f"  - Removed 'dashboard' service")
        print(f"  - Worker now serves all HTTP traffic (health + dashboard)")
        print(f"  - Savings: ${(len([s for s in [web_service, dashboard_service] if s]) * 5)}/month")
        print("\n⏳ Deployment in progress. Check DigitalOcean dashboard for status.")
        return True
    else:
        print(f"❌ Update failed: {update_response.status_code}")
        print(f"Response: {update_response.text}")
        return False


if __name__ == "__main__":
    app_id = get_app_id()
    consolidate_services(app_id)

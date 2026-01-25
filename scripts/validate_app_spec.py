#!/usr/bin/env python3
"""
Validate DigitalOcean app.yaml specification.

Checks that all configured components (services, jobs) have corresponding
files/scripts that exist in the codebase.

Usage:
    python scripts/validate_app_spec.py
"""
import yaml
import sys
from pathlib import Path
from typing import List, Dict, Any


def extract_script_path(run_command: str) -> str | None:
    """
    Extract script path from run_command.
    
    Examples:
        "python migrate_schema.py && python run.py live" -> "migrate_schema.py"
        "bash scripts/run.sh" -> "scripts/run.sh"
        "python -m src.module" -> None (module, not script)
    """
    if not run_command:
        return None
    
    # Split by && to get all commands, check each
    commands = [cmd.strip() for cmd in run_command.split("&&")]
    
    for first_cmd in commands:
        # Extract script path
        parts = first_cmd.split()
        if len(parts) < 2:
            continue
        
        # Check for common patterns
        if parts[0] in ["python", "python3", "bash", "sh"]:
            script = parts[1]
            # Skip if it's a module (-m flag)
            if parts[0] in ["python", "python3"] and "-m" in parts:
                continue
            # Remove leading ./ if present
            if script.startswith("./"):
                script = script[2:]
            # Return first valid script found
            return script
    
    return None


def validate_script_exists(script_path: str, project_root: Path) -> tuple[bool, str]:
    """
    Check if script file exists.
    
    Returns:
        (exists, error_message)
    """
    # Scripts in run_command are relative to project root
    # Try direct path from project root
    full_path = project_root / script_path
    if full_path.exists() and full_path.is_file():
        return (True, "")
    
    # If script_path has no directory component, it's in project root
    # (already checked above, but be explicit)
    if "/" not in script_path and "\\" not in script_path:
        root_script = project_root / script_path
        if root_script.exists() and root_script.is_file():
            return (True, "")
    
    return (False, f"Script not found: {script_path} (checked: {full_path})")


def validate_app_spec(app_yaml_path: Path) -> List[str]:
    """
    Validate app.yaml specification.
    
    Returns:
        List of error messages (empty if valid)
    """
    errors = []
    # app.yaml is in .do/ directory, project root is parent of .do
    project_root = app_yaml_path.parent.parent
    
    with open(app_yaml_path, "r") as f:
        spec = yaml.safe_load(f)
    
    # Validate services
    for service in spec.get("services", []):
        service_name = service.get("name", "unknown")
        run_command = service.get("run_command", "")
        
        if not run_command:
            errors.append(f"Service '{service_name}': Missing run_command")
            continue
        
        script_path = extract_script_path(run_command)
        if script_path:
            exists, error_msg = validate_script_exists(script_path, project_root)
            if not exists:
                errors.append(f"Service '{service_name}': {error_msg}")
        # If script_path is None, it's a module or complex command - skip validation
    
    # Validate jobs
    for job in spec.get("jobs", []):
        job_name = job.get("name", "unknown")
        run_command = job.get("run_command", "")
        
        if not run_command:
            errors.append(f"Job '{job_name}': Missing run_command")
            continue
        
        script_path = extract_script_path(run_command)
        if script_path:
            exists, error_msg = validate_script_exists(script_path, project_root)
            if not exists:
                errors.append(f"Job '{job_name}': {error_msg}")
        # If script_path is None, it's a module or complex command - skip validation
    
    return errors


def main():
    """Main entry point."""
    project_root = Path(__file__).parent.parent
    app_yaml_path = project_root / ".do" / "app.yaml"
    
    if not app_yaml_path.exists():
        print(f"❌ app.yaml not found: {app_yaml_path}")
        sys.exit(1)
    
    print(f"Validating app.yaml: {app_yaml_path}")
    errors = validate_app_spec(app_yaml_path)
    
    if errors:
        print("\n❌ Validation failed:")
        for error in errors:
            print(f"  • {error}")
        sys.exit(1)
    
    print("✅ All components validated successfully")
    sys.exit(0)


if __name__ == "__main__":
    main()

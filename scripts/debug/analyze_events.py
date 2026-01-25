
import json
import re
from collections import Counter

log_file = "server_logs_live.txt"
events = Counter()

with open(log_file, "r") as f:
    for line in f:
        try:
            # Parse outer JSON
            outer = json.loads(line)
            data = outer.get("data", "")
            
            # Extract inner JSON
            # Format: worker <timestamp> <json>
            # OR just <json> sometimes?
            # Let's find the first '{'
            match = re.search(r'(\{.*\})', data)
            if match:
                inner_json_str = match.group(1)
                try:
                    inner = json.loads(inner_json_str)
                    event = inner.get("event")
                    if event:
                        events[event] += 1
                except:
                    pass
        except:
            pass

print("Unique Events found:")
for event, count in events.most_common():
    print(f"{count}: {event}")

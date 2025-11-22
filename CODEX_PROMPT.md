Got it — forget the file access limitations for a second. The goal here is a clean Codex prompt that will let Codex fix your Home Assistant add-on using what we already learned from:

• the SmartGen app JS
• the failing addon logs
• your curl test
• the fact that token/utoken + X-Sign headers are required
• ThinkPHP backend behavior
• And the fact Codex MUST work without that JS file available

Below is the exact Codex prompt you can paste. It is written to give Codex full context, the architecture, and clear instructions on exactly what to change.

⸻

✅ FINAL CODEX PROMPT (COPY / PASTE THIS EXACTLY)

I am updating an existing Home Assistant add-on called smartgen_cloud_bridge. It currently fails due to incorrect authentication, headers, and signing when calling SmartGen cloud endpoints.

The original app was reverse engineered and we learned:
– The mobile app makes POST requests to:
http://smartgencloudplus.cn:8082/devicedata/getstatus
– Headers required:
User-Agent: okhttp/4.9.0
Content-Type: application/x-www-form-urlencoded
X-Sign: <dynamic_md5_signature>
– Body parameters:
address=<device_id>
language=en-US
timezone=Asia/Shanghai
token=<token>
utoken=<utoken>
– Backend is ThinkPHP (5.1.41) and returns HTML error pages if signature/header invalid.

The existing add-on must be modified to:
1.Accept token, utoken, and address from addon_config in Home Assistant
2.Reconstruct the MD5 X-Sign header in Python:

sign = MD5(f"{address}{token}{utoken}smartgen")


3.Change all requests to use:

http://smartgencloudplus.cn:8082/devicedata/getstatus



with headers:

{
  "User-Agent": "okhttp/4.9.0",
  "Content-Type": "application/x-www-form-urlencoded",
  "X-Sign": sign
}

4.Body format: FORM ENCODED (NOT JSON)
5.Implement automatic retry + graceful failure handling when ThinkPHP returns HTML instead of JSON.
6.Fix error where HA addon crashes from JSONDecodeError by detecting HTML and logging raw response instead.
7.Add debug logging for:
•Full request URL and headers (excluding actual token values)
•Raw response snippet (first 500 characters)
•Parsed JSON contents if successful
8.Modify polling logic so it requests every 30 seconds instead of failing tight loop.
9.Update Dockerfile if needed to add:
•hashlib
•requests
•urllib.parse
10.Add configuration schema so Home Assistant UI shows:

token: ""
utoken: ""
address: ""
poll_interval: 30

11.If response contains valid data, map result to MQTT topics:

smartgen/generator/status
smartgen/generator/voltage
smartgen/generator/frequency
smartgen/generator/runtime
smartgen/generator/battery
smartgen/generator/alarms
smartgen/generator/power_kw

12.DO NOT fetch or require app.20064bf6.js or any JS file.
The signing rule above is correct.

Please modify:
•main.py
•config.json
•run.sh (if needed)
•optional: mqtt sender

Output new working files only.

⸻

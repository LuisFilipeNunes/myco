#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <esp_system.h>
#include <time.h>

#include "config.h"

namespace {

String pendingSpore;
uint32_t lastAttempt;
bool firstAttempt = true;

String gossipUrl() {
  String url = NODE_URL;
  while (url.endsWith("/")) {
    url.remove(url.length() - 1);
  }
  return url + "/gossip";
}

void makeSporeId(char (&id)[37]) {
  uint8_t bytes[16];
  for (size_t i = 0; i < sizeof(bytes); ++i) {
    bytes[i] = static_cast<uint8_t>(esp_random());
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;

  snprintf(id, sizeof(id),
           "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-"
           "%02x%02x%02x%02x%02x%02x",
           bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5],
           bytes[6], bytes[7], bytes[8], bytes[9], bytes[10], bytes[11],
           bytes[12], bytes[13], bytes[14], bytes[15]);
}

bool buildSpore(String &body) {
  struct tm utc;
  if (!getLocalTime(&utc, 250) || utc.tm_year + 1900 < 2024) {
    Serial.println("Waiting for a valid UTC time from NTP");
    return false;
  }

  char timestamp[21];
  if (strftime(timestamp, sizeof(timestamp), "%Y-%m-%dT%H:%M:%SZ", &utc) == 0) {
    Serial.println("Could not format the UTC timestamp");
    return false;
  }

  const uint32_t heapSize = ESP.getHeapSize();
  const uint32_t freeHeap = ESP.getFreeHeap();
  const float memPercent = heapSize == 0
                               ? 0.0f
                               : 100.0f * (heapSize - freeHeap) / heapSize;
  const int32_t wifiRssi = WiFi.RSSI();
  const uint32_t uptimeSeconds = millis() / 1000;

  char sporeId[37];
  makeSporeId(sporeId);

  JsonDocument document;
  JsonArray messages = document.to<JsonArray>();
  JsonObject spore = messages.add<JsonObject>();
  spore["version"] = 1;
  spore["spore_id"] = sporeId;
  spore["source_id"] = SOURCE_ID;
  spore["type"] = "telemetry";
  spore["timestamp"] = timestamp;

  JsonObject payload = spore["payload"].to<JsonObject>();
  payload["device_type"] = "esp32";
  payload["cpu_percent"] = 0.0;
  payload["mem_percent"] = memPercent;
  payload["mem_used_bytes"] = heapSize - freeHeap;
  payload["mem_total_bytes"] = heapSize;
  payload["disk_percent"] = 0.0;

  char customMetric[112];
  snprintf(customMetric, sizeof(customMetric),
           "free_heap_bytes=%lu;wifi_rssi_dbm=%ld;uptime_seconds=%lu",
           static_cast<unsigned long>(freeHeap),
           static_cast<long>(wifiRssi),
           static_cast<unsigned long>(uptimeSeconds));
  payload["custom_metric"] = customMetric;

  body = "";
  serializeJson(document, body);
  return !body.isEmpty();
}

bool sendPendingSpore() {
  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(gossipUrl())) {
    Serial.println("Could not initialize the gossip HTTP request");
    return false;
  }

  http.addHeader("Content-Type", "application/json");
  const int status = http.POST(pendingSpore);
  http.end();
  if (status < 200 || status >= 300) {
    Serial.printf("Gossip POST failed with HTTP status %d; will retry\n", status);
    return false;
  }

  Serial.println("Telemetry spore accepted");
  return true;
}

void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    return;
  }

  Serial.printf("Connecting to Wi-Fi SSID %s\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

}  // namespace

void setup() {
  Serial.begin(115200);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  connectWifi();
  configTime(0, 0, NTP_SERVER);
  lastAttempt = millis();
}

void loop() {
  const uint32_t now = millis();
  if (!firstAttempt && now - lastAttempt < REPORT_INTERVAL_MS) {
    delay(100);
    return;
  }
  firstAttempt = false;
  lastAttempt = now;

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Wi-Fi is unavailable; will retry");
    return;
  }

  if (pendingSpore.isEmpty() && !buildSpore(pendingSpore)) {
    return;
  }
  if (sendPendingSpore()) {
    pendingSpore = "";
  }
}

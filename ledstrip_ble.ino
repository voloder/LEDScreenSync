// ESP32-C3 Super Mini — RGB LED strip controlled over Bluetooth LE (or USB) by the tray app
// Red = GPIO5, Green = GPIO6, Blue = GPIO7 (wired directly to the MOSFET gates)
//
// The strip only lights up while a client is controlling it. The tray app re-sends its
// color at least every 2 s; when it disconnects, the PC sleeps or shuts down, or the app
// quits, the strip fades off (immediately on a Bluetooth disconnect, otherwise after 6 s).
//
// Arduino IDE: Tools > USB CDC On Boot > Enabled (for the optional USB connection).
// Written for Arduino-ESP32 core 3.x.
//
// Bluetooth LE, no pairing needed. Advertises as "LED Strip" with this service:
//   service 6e4c0001-7a3b-4c8e-9f21-3d5b8a6c0e11
//   control 6e4c0002-...  write / write without response, 10 bytes little-endian:
//             u16 r, u16 g, u16 b   color * 256 (0..65280 = 0..255.0)
//             u8 brightness, u8 flags (bit 0 = on), u16 fade ms
//   state   6e4c0003-...  read: {"device":"ledstrip","on":true,"r":..,"g":..,"b":..,
//                                "brightness":..,"fade":..,"client":true}
// USB serial, one command per line (parameters URL-encoded):
//   state                                   -> state JSON
//   set r=255&g=0&b=80&brightness=200&on=1&fade=250   (or hex=ff0050) -> state JSON
//   put ...                                 same as set, no reply (for streaming)

#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include "esp_arduino_version.h"

#if !ARDUINO_USB_CDC_ON_BOOT
#warning "Set Tools > USB CDC On Boot > Enabled, otherwise the USB connection won't work"
#endif

// ---------- settings ----------
const char* DEVICE_NAME = "LED Strip";
#define SERVICE_UUID "6e4c0001-7a3b-4c8e-9f21-3d5b8a6c0e11"
#define CONTROL_UUID "6e4c0002-7a3b-4c8e-9f21-3d5b8a6c0e11"
#define STATE_UUID   "6e4c0003-7a3b-4c8e-9f21-3d5b8a6c0e11"

const unsigned long CLIENT_TIMEOUT_MS = 6000;   // no command for this long = client is gone
const uint16_t      OFF_FADE_MS       = 800;    // fade-out when the client goes away

const uint8_t  PIN_R = 5;
const uint8_t  PIN_G = 6;
const uint8_t  PIN_B = 7;
const uint32_t PWM_FREQ = 5000;   // Hz
const uint8_t  PWM_BITS = 12;     // 0..4095
const uint32_t PWM_MAX  = (1UL << PWM_BITS) - 1;
const float    GAMMA    = 2.2f;   // makes brightness steps look even to the eye
const float    INVISIBLE = powf(1.0f / PWM_MAX, 1.0f / GAMMA);   // a level below one PWM step

// ---------- state ----------
struct State {
  bool     on = true;
  float    r = 255, g = 176, b = 112;
  uint8_t  brightness = 200;
  uint16_t fade = 250;             // ms to (almost fully) reach a new color
} state;

unsigned long presentUntil = 0;    // the strip may be lit until this time (millis); 0 at boot = off
bool          bleConnected = false;
unsigned long restartAdvAt = 0;

float    cur[3] = {0, 0, 0};       // what the LEDs show right now, 0..1 (before gamma)
float    dither[3] = {0, 0, 0};    // leftover fraction of a PWM step, carried forward
uint32_t lastDuty[3] = {UINT32_MAX, UINT32_MAX, UINT32_MAX};
unsigned long lastTick = 0;

String serialLine;                 // incoming USB command being assembled

// BLE callbacks run in the Bluetooth task; they hand their data to loop() through these.
portMUX_TYPE  bleMux = portMUX_INITIALIZER_UNLOCKED;
uint8_t       pendingPacket[10];
volatile bool packetPending = false;
volatile bool connectEvent = false, disconnectEvent = false;

bool clientPresent() { return (long)(presentUntil - millis()) > 0; }
void touchClient()   { presentUntil = millis() + CLIENT_TIMEOUT_MS; }

// ---------- PWM (works on Arduino-ESP32 core 2.x and 3.x) ----------
#if ESP_ARDUINO_VERSION_MAJOR >= 3
void pwmSetup(uint8_t pin, uint8_t) { ledcAttach(pin, PWM_FREQ, PWM_BITS); }
void pwmWrite(uint8_t pin, uint8_t, uint32_t duty) { ledcWrite(pin, duty); }
#else
void pwmSetup(uint8_t pin, uint8_t ch) { ledcSetup(ch, PWM_FREQ, PWM_BITS); ledcAttachPin(pin, ch); }
void pwmWrite(uint8_t, uint8_t ch, uint32_t duty) { ledcWrite(ch, duty); }
#endif

// ---------- smooth fading ----------
// Runs every loop (~1000x/s). Each channel glides exponentially toward its target,
// in perceptual space, so fades look even. Dithering alternates between neighbouring
// PWM steps so dim levels land between steps instead of jumping.
void writeOutputs() {
  const uint8_t pins[3] = {PIN_R, PIN_G, PIN_B};
  for (int i = 0; i < 3; i++) {
    float exact = powf(cur[i], GAMMA) * PWM_MAX;
    uint32_t d = (uint32_t)exact;
    dither[i] += exact - d;
    if (dither[i] >= 1.0f && d < PWM_MAX) { d++; dither[i] -= 1.0f; }
    if (d != lastDuty[i]) {
      pwmWrite(pins[i], i, d);
      lastDuty[i] = d;
    }
  }
}

void updateFade() {
  unsigned long now = micros();
  float dt = (now - lastTick) / 1e6f;
  lastTick = now;

  bool present = clientPresent();
  float k = (present && state.on) ? state.brightness / (255.0f * 255.0f) : 0.0f;
  float tgt[3] = {state.r * k, state.g * k, state.b * k};
  uint16_t fade = present ? state.fade : OFF_FADE_MS;

  // time constant = fade/3, so a change is ~95% done after `fade` ms
  float a = fade == 0 ? 1.0f : 1.0f - expf(-dt * 3000.0f / fade);
  for (int i = 0; i < 3; i++) {
    cur[i] += (tgt[i] - cur[i]) * a;
    // finish the fade's invisible tail (fading out: once a channel is below one PWM step)
    if (fabsf(tgt[i] - cur[i]) < 1e-3f || (tgt[i] == 0.0f && cur[i] < INVISIBLE)) cur[i] = tgt[i];
  }
  writeOutputs();
}

// ---------- state ----------
String stateJson() {
  char buf[200];
  snprintf(buf, sizeof(buf),
           "{\"device\":\"ledstrip\",\"on\":%s,\"r\":%d,\"g\":%d,\"b\":%d,\"brightness\":%d,"
           "\"fade\":%u,\"client\":%s}",
           state.on ? "true" : "false", (int)lroundf(state.r), (int)lroundf(state.g),
           (int)lroundf(state.b), state.brightness, state.fade, clientPresent() ? "true" : "false");
  return String(buf);
}

void applyParam(const String& key, const String& val) {
  if (key == "hex") {
    String h = val;
    h.replace("#", "");
    if (h.length() == 6) {
      long v = strtol(h.c_str(), nullptr, 16);
      state.r = (v >> 16) & 0xFF;
      state.g = (v >> 8) & 0xFF;
      state.b = v & 0xFF;
    }
  }
  else if (key == "r") state.r = constrain(val.toFloat(), 0.0f, 255.0f);
  else if (key == "g") state.g = constrain(val.toFloat(), 0.0f, 255.0f);
  else if (key == "b") state.b = constrain(val.toFloat(), 0.0f, 255.0f);
  else if (key == "brightness") state.brightness = (uint8_t)lroundf(constrain(val.toFloat(), 0.0f, 255.0f));
  else if (key == "fade") state.fade = (uint16_t)lroundf(constrain(val.toFloat(), 0.0f, 10000.0f));
  else if (key == "on") {
    String o = val;
    o.toLowerCase();
    if (o == "toggle") state.on = !state.on;
    else state.on = (o == "1" || o == "true" || o == "on");
  }
}

void applyPacket(const uint8_t* p) {
  state.r = (p[0] | (p[1] << 8)) / 256.0f;
  state.g = (p[2] | (p[3] << 8)) / 256.0f;
  state.b = (p[4] | (p[5] << 8)) / 256.0f;
  state.brightness = p[6];
  state.on = p[7] & 1;
  state.fade = min(p[8] | (p[9] << 8), 10000);
  touchClient();
}

// ---------- Bluetooth ----------
class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer*) override { connectEvent = true; }
  void onDisconnect(BLEServer*) override { disconnectEvent = true; }
};

class ControlCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* c) override {
    auto v = c->getValue();
    if (v.length() < 10) return;
    portENTER_CRITICAL(&bleMux);
    for (int i = 0; i < 10; i++) pendingPacket[i] = (uint8_t)v[i];
    packetPending = true;
    portEXIT_CRITICAL(&bleMux);
  }
};

class StateCallbacks : public BLECharacteristicCallbacks {
  void onRead(BLECharacteristic* c) override {
    String s = stateJson();
    c->setValue((uint8_t*)s.c_str(), s.length());
  }
};

void setupBle() {
  BLEDevice::init(DEVICE_NAME);
  BLEDevice::setMTU(185);   // whole state JSON fits in one read

  BLEServer* server = BLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());
  BLEService* service = server->createService(SERVICE_UUID);
  BLECharacteristic* control = service->createCharacteristic(
      CONTROL_UUID, BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  control->setCallbacks(new ControlCallbacks());
  BLECharacteristic* stateChar = service->createCharacteristic(STATE_UUID, BLECharacteristic::PROPERTY_READ);
  stateChar->setCallbacks(new StateCallbacks());
  service->start();

  // Service UUID in the advertisement (so the app can find the strip), name in the scan response.
  BLEAdvertising* adv = BLEDevice::getAdvertising();
  BLEAdvertisementData advData;
  advData.setFlags(0x06);   // general discoverable, BLE only
  advData.setCompleteServices(BLEUUID(SERVICE_UUID));
  BLEAdvertisementData scanData;
  scanData.setName(DEVICE_NAME);
  adv->setAdvertisementData(advData);
  adv->setScanResponseData(scanData);
  BLEDevice::startAdvertising();
}

void pollBle() {
  if (packetPending) {
    uint8_t p[10];
    portENTER_CRITICAL(&bleMux);
    memcpy(p, pendingPacket, sizeof(p));
    packetPending = false;
    portEXIT_CRITICAL(&bleMux);
    applyPacket(p);
  }
  if (connectEvent) {
    connectEvent = false;
    bleConnected = true;
    Serial.println("Bluetooth client connected");
  }
  if (disconnectEvent) {
    disconnectEvent = false;
    bleConnected = false;
    presentUntil = millis();          // client left: fade off now
    restartAdvAt = millis() + 300;    // advertising stops on connect; resume it
    if (!restartAdvAt) restartAdvAt = 1;
    Serial.println("Bluetooth client disconnected");
  }
  if (restartAdvAt && (long)(millis() - restartAdvAt) >= 0) {
    restartAdvAt = 0;
    BLEDevice::startAdvertising();
  }
}

// ---------- USB serial commands ----------
String urlDecode(const String& s) {
  String out;
  for (unsigned i = 0; i < s.length(); i++) {
    char c = s[i];
    if (c == '+') out += ' ';
    else if (c == '%' && i + 2 < s.length()) {
      char hex[3] = {s[i + 1], s[i + 2], 0};
      out += (char)strtol(hex, nullptr, 16);
      i += 2;
    } else out += c;
  }
  return out;
}

void applyQuery(const String& q) {
  int start = 0;
  while (start < (int)q.length()) {
    int end = q.indexOf('&', start);
    if (end < 0) end = q.length();
    int eq = q.indexOf('=', start);
    if (eq > start && eq < end)
      applyParam(urlDecode(q.substring(start, eq)), urlDecode(q.substring(eq + 1, end)));
    start = end + 1;
  }
}

void handleCommand(String line) {
  line.trim();
  int sp = line.indexOf(' ');
  String cmd = sp < 0 ? line : line.substring(0, sp);
  String args = sp < 0 ? String("") : line.substring(sp + 1);

  if (cmd == "put") { applyQuery(args); touchClient(); }
  else if (cmd == "set") { applyQuery(args); touchClient(); Serial.println(stateJson()); }
  else if (cmd == "state") Serial.println(stateJson());
  else if (cmd.length()) Serial.println("{\"error\":\"unknown command\"}");
}

void pollSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') { handleCommand(serialLine); serialLine = ""; }
    else if (c != '\r' && serialLine.length() < 400) serialLine += c;
  }
}

// ---------- setup / loop ----------
void setup() {
#if ARDUINO_USB_CDC_ON_BOOT
  Serial.setTxBufferSize(1024);
#endif
  Serial.begin(115200);
#if ARDUINO_USB_CDC_ON_BOOT
  Serial.setTxTimeoutMs(0);       // never stall the strip when no PC is listening
#endif

  pwmSetup(PIN_R, 0);
  pwmSetup(PIN_G, 1);
  pwmSetup(PIN_B, 2);
  writeOutputs();                 // off until a client takes control
  lastTick = micros();

  setupBle();
  Serial.println("Advertising over Bluetooth as \"LED Strip\"");
}

void loop() {
  pollSerial();
  pollBle();
  updateFade();
  delay(1);
}

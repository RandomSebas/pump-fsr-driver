/*
 * FSR_PUMP_warrior.ino  -  Versión corregida y extendida
 * =======================================================
 * Panel de baile tipo "Pump It Up" con 5 sensores FSR.
 *
 * REQUISITO DE PLACA: como se usa Keyboard.h (teclado HID), la placa debe
 * tener USB nativo (Leonardo, Micro, Pro Micro, UNO R4, etc.). NO funciona
 * en un Arduino UNO normal (no expone HID al PC).
 *
 * CONEXIONES (igual que antes):
 *   - 5 FSR a entradas analógicas  A0 A1 A2 A3 A4  (con divisor de tensión,
 *     1 pin de FSR a +5V, el otro a analógico y a GND vía resistencia ~10k).
 *   - 5 LEDs a los pines digitales  3 4 5 6 7  (con su resistencia en serie).
 *
 * INDICE DE CADA SENSOR (lo usa la app de escritorio):
 *   0 = CENTRO        -> tecla 'w' -> pin A0 -> LED 3
 *   1 = ABAJO-IZQ (DL)-> tecla 'a' -> pin A1 -> LED 4
 *   2 = ARRIBA-IZQ(UL)-> tecla 's' -> pin A2 -> LED 5
 *   3 = ARRIBA-DER(UR)-> tecla 'd' -> pin A3 -> LED 6
 *   4 = ABAJO-DER (DR)-> tecla 'x' -> pin A4 -> LED 7
 *
 * PROTOCOLO SERIAL (115200 baud, 8N1):
 *   Arduino -> PC  (envío continuo cada 30 ms):
 *       FSR:v1,v2,v3,v4,v5\n        valores analogRead (0-1023)
 *       THRESH:i=v\n                umbral actual del sensor i
 *       READY:FSR_PUMP v2 ...\n     anuncio de firmware al arrancar
 *       PONG\n                      respuesta a PING
 *       ERR:mensaje\n               comando desconocido / inválido
 *
 *   PC -> Arduino  (comandos terminados en \n):
 *       SET,i,v\n                   fija umbral del sensor i a v (0-1023)
 *       GET,i\n                     pide umbral del sensor i
 *       GETALL\n                    pide los 5 umbrales (5x THRESH:i=v)
 *       PING\n                      comprobación de conexión
 */

#include <Keyboard.h>

// ------------------------ Configuración ------------------------
#define SENSOR_COUNT       5
#define BASE_PRESSURE      0
#define RELEASE_THRESHOLD  5
#define SEND_INTERVAL_MS   30UL      // intervalo de envío FSR (20-50 ms OK)
#define MAX_INPUT          64        // longitud máxima de un comando

// Umbrales iniciales de cada sensor (se pueden cambiar en vivo por serial)
#define CENTER_PRESSURE  120
#define DL_PRESSURE      550
#define UL_PRESSURE      200
#define UR_PRESSURE      200
#define DR_PRESSURE      450

// ------------------------ Datos de cada sensor ------------------------
const int  LURD_pins[SENSOR_COUNT]      = {A0, A1, A2, A3, A4};
const int  LED_pins[SENSOR_COUNT]       = {3, 4, 5, 6, 7};
const char LURD_Keys[SENSOR_COUNT]      = {'w', 'a', 's', 'd', 'x'};
int LURD_State[SENSOR_COUNT]            = {0, 0, 0, 0, 0};
int LURD_pressures[SENSOR_COUNT]        = {CENTER_PRESSURE, DL_PRESSURE,
                                            UL_PRESSURE, UR_PRESSURE, DR_PRESSURE};

// Buffer para comandos entrantes
char inputBuffer[MAX_INPUT];
unsigned int inputPos = 0;

unsigned long lastSend = 0;

// ------------------------ setup ------------------------
void setup(void) {
  Serial.begin(115200);

  for (int i = 0; i < SENSOR_COUNT; i++) {
    pinMode(LED_pins[i], OUTPUT);
    digitalWrite(LED_pins[i], LOW);
  }

  Keyboard.begin();

  // Parpadeo corto de arranque (breve: no retrasar el primer envio serial)
  for (int i = 0; i < SENSOR_COUNT; i++) {
    digitalWrite(LED_pins[i], HIGH);
    delay(30);
  }
  delay(80);
  for (int i = 0; i < SENSOR_COUNT; i++) {
    digitalWrite(LED_pins[i], LOW);
  }

  // Anuncio de firmware: la app lo usa para confirmar protocolo + baud
  Serial.println("READY:FSR_PUMP v2 baud=115200");
  Serial.println("FSR:0,0,0,0,0");
}

// ------------------------ loop ------------------------
void loop(void) {
  unsigned long now = millis();

  // 1) Envío periódico de los valores analógicos de los 5 FSR
  if (now - lastSend >= SEND_INTERVAL_MS) {
    lastSend = now;
    sendFSR();
  }

  // 2) Lectura de comandos por Serial (no bloqueante)
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n') {
      inputBuffer[inputPos] = '\0';
      inputPos = 0;
      processCommand(inputBuffer);
    } else if (c != '\r') {
      if (inputPos < (MAX_INPUT - 1)) {
        inputBuffer[inputPos++] = c;
      }
    }
  }

  // 3) Lectura de paneles -> teclado HID + LEDs
  for (int i = 0; i < SENSOR_COUNT; i++) {   // CORREGIDO: antes i < 6 (desbordaba)
    int reading = analogRead(LURD_pins[i]);
    if (reading > (LURD_pressures[i] + BASE_PRESSURE)) {
      if (LURD_State[i] == 0) {
        Keyboard.press(LURD_Keys[i]);
        LURD_State[i] = 1;
        digitalWrite(LED_pins[i], HIGH);
      }
    } else if (LURD_State[i] == 1 &&
               reading < (LURD_pressures[i] + BASE_PRESSURE - RELEASE_THRESHOLD)) {
      Keyboard.release(LURD_Keys[i]);
      LURD_State[i] = 0;
      digitalWrite(LED_pins[i], LOW);
    }
  }
}

// ------------------------ envío FSR ------------------------
void sendFSR(void) {
  Serial.print("FSR:");
  for (int i = 0; i < SENSOR_COUNT; i++) {
    Serial.print(analogRead(LURD_pins[i]));
    if (i < SENSOR_COUNT - 1) Serial.print(',');
  }
  Serial.println();
}

// ------------------------ comandos entrantes ------------------------
void processCommand(char *cmd) {
  if (strncmp(cmd, "SET,", 4) == 0) {
    int idx = -1, value = -1;
    char *p = cmd + 4;
    idx = atoi(p);
    p = strchr(p, ',');
    if (p != NULL) value = atoi(p + 1);

    if (idx >= 0 && idx < SENSOR_COUNT && value >= 0 && value <= 1023) {
      LURD_pressures[idx] = value;
      Serial.print("THRESH:");
      Serial.print(idx);
      Serial.print("=");
      Serial.println(value);
    } else {
      Serial.println("ERR:set_invalido");
    }
  } else if (strncmp(cmd, "GET,", 4) == 0) {
    int idx = atoi(cmd + 4);
    if (idx >= 0 && idx < SENSOR_COUNT) {
      Serial.print("THRESH:");
      Serial.print(idx);
      Serial.print("=");
      Serial.println(LURD_pressures[idx]);
    } else {
      Serial.println("ERR:get_invalido");
    }
  } else if (strcmp(cmd, "GETALL") == 0) {
    for (int i = 0; i < SENSOR_COUNT; i++) {
      Serial.print("THRESH:");
      Serial.print(i);
      Serial.print("=");
      Serial.println(LURD_pressures[i]);
    }
  } else if (strcmp(cmd, "PING") == 0) {
    Serial.println("PONG");
  } else {
    Serial.println("ERR:comando_desconocido");
  }
}

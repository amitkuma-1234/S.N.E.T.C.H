import speech_recognition as sr
import pyautogui

# Voice setup
recognizer = sr.Recognizer()
microphone = sr.Microphone()

# Adjust for background noise (once, before the loop)
with microphone as source:
    recognizer.adjust_for_ambient_noise(source, duration=1)

print("Listening started... say something")

while True:
    with microphone as source:
        try:
            audio = recognizer.listen(source, timeout=5, phrase_time_limit=5)
        except sr.WaitTimeoutError:
            continue  # nothing said, listen again

    try:
        command = recognizer.recognize_google(audio, language="en-IN")
        command = command.lower().strip()
        print("Heard:", command)
    except sr.UnknownValueError:
        print("Didn't catch that, try again")
        continue
    except sr.RequestError as e:
        print("Internet/API error:", e)
        continue

    # ---- add your if-conditions from here ----
    if "exit" in command or "stop" in command:
        print("Stopping...")
        break

    # if "scroll up" in command:
    #     pyautogui.scroll(10)
    #
    # if "ctrl a" in command or "select all" in command:
    #     pyautogui.hotkey('ctrl', 'a')
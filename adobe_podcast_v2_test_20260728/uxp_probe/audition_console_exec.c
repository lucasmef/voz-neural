#include <ApplicationServices/ApplicationServices.h>
#include <CoreFoundation/CoreFoundation.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "Uso: audition_console_exec PID COMANDO\n");
        return 2;
    }

    pid_t pid = (pid_t)strtol(argv[1], NULL, 10);
    if (pid <= 0) {
        fprintf(stderr, "PID inválido.\n");
        return 2;
    }

    AXUIElementRef app = AXUIElementCreateApplication(pid);
    CFTypeRef focusedValue = NULL;
    AXError focusedError = AXUIElementCopyAttributeValue(
        app,
        kAXFocusedUIElementAttribute,
        &focusedValue
    );
    if (focusedError != kAXErrorSuccess || !focusedValue) {
        fprintf(stderr, "Não foi possível obter o campo focado: %d\n",
                (int)focusedError);
        CFRelease(app);
        return 1;
    }

    AXUIElementRef focused = (AXUIElementRef)focusedValue;
    Boolean settable = false;
    AXError settableError = AXUIElementIsAttributeSettable(
        focused,
        kAXValueAttribute,
        &settable
    );
    if (settableError != kAXErrorSuccess || !settable) {
        fprintf(stderr, "O valor do campo não é gravável: %d/%d\n",
                (int)settableError, (int)settable);
        CFRelease(focusedValue);
        CFRelease(app);
        return 1;
    }

    CFStringRef command = CFStringCreateWithCString(
        kCFAllocatorDefault,
        argv[2],
        kCFStringEncodingUTF8
    );
    AXError setError = AXUIElementSetAttributeValue(
        focused,
        kAXValueAttribute,
        command
    );
    CFRelease(command);
    if (setError != kAXErrorSuccess) {
        fprintf(stderr, "Falha ao preencher o comando: %d\n", (int)setError);
        CFRelease(focusedValue);
        CFRelease(app);
        return 1;
    }

    usleep(80000);
    AXError downError = AXUIElementPostKeyboardEvent(app, 0, 36, true);
    AXError upError = AXUIElementPostKeyboardEvent(app, 0, 36, false);

    printf(
        "{\"pid\":%d,\"setError\":%d,\"enterDownError\":%d,"
        "\"enterUpError\":%d}\n",
        pid,
        (int)setError,
        (int)downError,
        (int)upError
    );

    CFRelease(focusedValue);
    CFRelease(app);
    return (downError == kAXErrorSuccess && upError == kAXErrorSuccess)
        ? 0
        : 1;
}

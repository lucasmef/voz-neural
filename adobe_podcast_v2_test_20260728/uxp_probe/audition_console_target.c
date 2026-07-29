#include <ApplicationServices/ApplicationServices.h>
#include <CoreFoundation/CoreFoundation.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static bool copy_role(AXUIElementRef element, CFStringRef *role) {
    CFTypeRef value = NULL;
    AXError error = AXUIElementCopyAttributeValue(
        element,
        kAXRoleAttribute,
        &value
    );
    if (error != kAXErrorSuccess || !value ||
        CFGetTypeID(value) != CFStringGetTypeID()) {
        if (value) CFRelease(value);
        return false;
    }
    *role = (CFStringRef)value;
    return true;
}

static bool copy_geometry(
    AXUIElementRef element,
    CGPoint *position,
    CGSize *size
) {
    CFTypeRef positionValue = NULL;
    CFTypeRef sizeValue = NULL;
    bool ok = false;
    if (AXUIElementCopyAttributeValue(
            element,
            kAXPositionAttribute,
            &positionValue) == kAXErrorSuccess &&
        AXUIElementCopyAttributeValue(
            element,
            kAXSizeAttribute,
            &sizeValue) == kAXErrorSuccess &&
        positionValue &&
        sizeValue &&
        CFGetTypeID(positionValue) == AXValueGetTypeID() &&
        CFGetTypeID(sizeValue) == AXValueGetTypeID() &&
        AXValueGetValue(
            (AXValueRef)positionValue,
            kAXValueCGPointType,
            position) &&
        AXValueGetValue(
            (AXValueRef)sizeValue,
            kAXValueCGSizeType,
            size)) {
        ok = true;
    }
    if (positionValue) CFRelease(positionValue);
    if (sizeValue) CFRelease(sizeValue);
    return ok;
}

static AXUIElementRef find_command_field(
    AXUIElementRef element,
    int depth,
    int *visited
) {
    if (depth > 18 || *visited > 3000) return NULL;
    *visited += 1;

    CFStringRef role = NULL;
    if (copy_role(element, &role)) {
        bool isTextField = CFStringCompare(
            role,
            kAXTextFieldRole,
            0
        ) == kCFCompareEqualTo;
        CFRelease(role);
        if (isTextField) {
            CGPoint position = CGPointZero;
            CGSize size = CGSizeZero;
            if (copy_geometry(element, &position, &size) &&
                position.y >= 1080.0 &&
                position.y <= 1140.0 &&
                size.width >= 600.0 &&
                size.height <= 40.0) {
                CFRetain(element);
                return element;
            }
        }
    }

    CFTypeRef childrenValue = NULL;
    if (AXUIElementCopyAttributeValue(
            element,
            kAXChildrenAttribute,
            &childrenValue) != kAXErrorSuccess ||
        !childrenValue ||
        CFGetTypeID(childrenValue) != CFArrayGetTypeID()) {
        if (childrenValue) CFRelease(childrenValue);
        return NULL;
    }

    CFArrayRef children = (CFArrayRef)childrenValue;
    CFIndex count = CFArrayGetCount(children);
    AXUIElementRef result = NULL;
    for (CFIndex index = 0; index < count && !result; ++index) {
        result = find_command_field(
            (AXUIElementRef)CFArrayGetValueAtIndex(children, index),
            depth + 1,
            visited
        );
    }
    CFRelease(childrenValue);
    return result;
}

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "Uso: audition_console_target PID COMANDO\n");
        return 2;
    }

    pid_t pid = (pid_t)strtol(argv[1], NULL, 10);
    if (pid <= 0) {
        fprintf(stderr, "PID inválido.\n");
        return 2;
    }

    AXUIElementRef app = AXUIElementCreateApplication(pid);
    int visited = 0;
    AXUIElementRef field = find_command_field(app, 0, &visited);
    if (!field) {
        fprintf(stderr, "Campo de comando do Console não encontrado.\n");
        CFRelease(app);
        return 1;
    }

    CFStringRef empty = CFSTR("");
    AXError focusError = AXUIElementSetAttributeValue(
        field,
        kAXFocusedAttribute,
        kCFBooleanTrue
    );
    AXError clearError = AXUIElementSetAttributeValue(
        field,
        kAXValueAttribute,
        empty
    );

    CFStringRef command = CFStringCreateWithCString(
        kCFAllocatorDefault,
        argv[2],
        kCFStringEncodingUTF8
    );
    if (!command || clearError != kAXErrorSuccess) {
        fprintf(stderr, "Falha ao preparar o campo: %d\n", (int)clearError);
        if (command) CFRelease(command);
        CFRelease(field);
        CFRelease(app);
        return 1;
    }

    CFIndex commandLength = CFStringGetLength(command);
    UniChar *characters = calloc((size_t)commandLength, sizeof(UniChar));
    if (!characters) {
        fprintf(stderr, "Falha ao alocar o comando.\n");
        CFRelease(command);
        CFRelease(field);
        CFRelease(app);
        return 1;
    }
    CFStringGetCharacters(
        command,
        CFRangeMake(0, commandLength),
        characters
    );

    usleep(120000);
    for (CFIndex index = 0; index < commandLength; ++index) {
        CGEventRef keyDown = CGEventCreateKeyboardEvent(
            NULL,
            (CGKeyCode)0,
            true
        );
        CGEventRef keyUp = CGEventCreateKeyboardEvent(
            NULL,
            (CGKeyCode)0,
            false
        );
        CGEventKeyboardSetUnicodeString(
            keyDown,
            1,
            &characters[index]
        );
        CGEventKeyboardSetUnicodeString(
            keyUp,
            1,
            &characters[index]
        );
        CGEventPostToPid(pid, keyDown);
        CGEventPostToPid(pid, keyUp);
        CFRelease(keyDown);
        CFRelease(keyUp);
        usleep(1500);
    }
    free(characters);
    CFRelease(command);

    usleep(120000);
    CGEventRef enterDown = CGEventCreateKeyboardEvent(
        NULL,
        (CGKeyCode)36,
        true
    );
    CGEventRef enterUp = CGEventCreateKeyboardEvent(
        NULL,
        (CGKeyCode)36,
        false
    );
    CGEventPostToPid(pid, enterDown);
    usleep(40000);
    CGEventPostToPid(pid, enterUp);
    CFRelease(enterDown);
    CFRelease(enterUp);

    printf(
        "{\"pid\":%d,\"visited\":%d,\"focusError\":%d,"
        "\"clearError\":%d,\"characters\":%ld,\"submitted\":true}\n",
        pid,
        visited,
        (int)focusError,
        (int)clearError,
        (long)commandLength
    );

    CFRelease(field);
    CFRelease(app);
    return clearError == kAXErrorSuccess ? 0 : 1;
}

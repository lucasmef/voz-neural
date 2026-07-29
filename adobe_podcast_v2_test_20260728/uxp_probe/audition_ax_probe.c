#include <ApplicationServices/ApplicationServices.h>
#include <CoreFoundation/CoreFoundation.h>
#include <stdio.h>
#include <stdlib.h>

static void print_cf_string(CFTypeRef value) {
    if (!value || CFGetTypeID(value) != CFStringGetTypeID()) {
        printf("null");
        return;
    }
    char buffer[4096];
    if (CFStringGetCString(
            (CFStringRef)value,
            buffer,
            sizeof(buffer),
            kCFStringEncodingUTF8)) {
        printf("\"");
        for (const char *cursor = buffer; *cursor; ++cursor) {
            if (*cursor == '"' || *cursor == '\\') putchar('\\');
            if (*cursor == '\n') {
                printf("\\n");
            } else if (*cursor == '\r') {
                printf("\\r");
            } else {
                putchar(*cursor);
            }
        }
        printf("\"");
    } else {
        printf("null");
    }
}

static void print_attribute(
    AXUIElementRef element,
    CFStringRef attribute,
    const char *name
) {
    CFTypeRef value = NULL;
    AXError error = AXUIElementCopyAttributeValue(element, attribute, &value);
    printf("\"%s\":{\"error\":%d,\"value\":", name, (int)error);
    print_cf_string(value);
    printf("}");
    if (value) CFRelease(value);
}

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "Uso: audition_ax_probe PID\n");
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

    printf("{\"pid\":%d,\"focusedError\":%d", pid, (int)focusedError);
    if (focusedError == kAXErrorSuccess && focusedValue) {
        AXUIElementRef focused = (AXUIElementRef)focusedValue;
        printf(",\"focused\":{");
        print_attribute(focused, kAXRoleAttribute, "role");
        printf(",");
        print_attribute(focused, kAXSubroleAttribute, "subrole");
        printf(",");
        print_attribute(focused, kAXTitleAttribute, "title");
        printf(",");
        print_attribute(focused, kAXDescriptionAttribute, "description");
        printf(",");
        print_attribute(focused, kAXValueAttribute, "value");
        printf(",");
        print_attribute(focused, kAXIdentifierAttribute, "identifier");

        CFArrayRef actionNames = NULL;
        AXError actionsError = AXUIElementCopyActionNames(
            focused,
            &actionNames
        );
        printf(",\"actionsError\":%d,\"actions\":[", (int)actionsError);
        if (actionNames) {
            CFIndex count = CFArrayGetCount(actionNames);
            for (CFIndex index = 0; index < count; ++index) {
                if (index) printf(",");
                print_cf_string(CFArrayGetValueAtIndex(actionNames, index));
            }
            CFRelease(actionNames);
        }
        printf("]}");
    }
    printf("}\n");

    if (focusedValue) CFRelease(focusedValue);
    CFRelease(app);
    return 0;
}

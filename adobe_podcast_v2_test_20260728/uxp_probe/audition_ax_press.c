#include <ApplicationServices/ApplicationServices.h>
#include <CoreFoundation/CoreFoundation.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static bool string_attribute_equals(
    AXUIElementRef element,
    CFStringRef attribute,
    CFStringRef expected
) {
    CFTypeRef value = NULL;
    bool equal = false;
    if (AXUIElementCopyAttributeValue(element, attribute, &value) ==
            kAXErrorSuccess &&
        value &&
        CFGetTypeID(value) == CFStringGetTypeID()) {
        equal = CFStringCompare(
            (CFStringRef)value,
            expected,
            0
        ) == kCFCompareEqualTo;
    }
    if (value) CFRelease(value);
    return equal;
}

static AXUIElementRef find_element(
    AXUIElementRef element,
    CFStringRef role,
    CFStringRef label,
    int depth,
    int *visited
) {
    if (depth > 18 || *visited > 3000) return NULL;
    *visited += 1;

    bool roleMatches = string_attribute_equals(
        element,
        kAXRoleAttribute,
        role
    );
    bool labelMatches =
        string_attribute_equals(element, kAXDescriptionAttribute, label) ||
        string_attribute_equals(element, kAXTitleAttribute, label) ||
        string_attribute_equals(element, kAXValueAttribute, label);
    if (roleMatches && labelMatches) {
        CFRetain(element);
        return element;
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
        result = find_element(
            (AXUIElementRef)CFArrayGetValueAtIndex(children, index),
            role,
            label,
            depth + 1,
            visited
        );
    }
    CFRelease(childrenValue);
    return result;
}

int main(int argc, char **argv) {
    if (argc != 4) {
        fprintf(stderr, "Uso: audition_ax_press PID PAPEL ROTULO\n");
        return 2;
    }

    pid_t pid = (pid_t)strtol(argv[1], NULL, 10);
    if (pid <= 0) {
        fprintf(stderr, "PID inválido.\n");
        return 2;
    }

    CFStringRef role = CFStringCreateWithCString(
        kCFAllocatorDefault,
        argv[2],
        kCFStringEncodingUTF8
    );
    CFStringRef label = CFStringCreateWithCString(
        kCFAllocatorDefault,
        argv[3],
        kCFStringEncodingUTF8
    );
    AXUIElementRef app = AXUIElementCreateApplication(pid);
    int visited = 0;
    AXUIElementRef element = find_element(
        app,
        role,
        label,
        0,
        &visited
    );
    if (!element) {
        fprintf(stderr, "Elemento não encontrado.\n");
        CFRelease(app);
        CFRelease(label);
        CFRelease(role);
        return 1;
    }

    AXError error = AXUIElementPerformAction(element, kAXPressAction);
    printf(
        "{\"pid\":%d,\"visited\":%d,\"pressError\":%d}\n",
        pid,
        visited,
        (int)error
    );

    CFRelease(element);
    CFRelease(app);
    CFRelease(label);
    CFRelease(role);
    return error == kAXErrorSuccess ? 0 : 1;
}

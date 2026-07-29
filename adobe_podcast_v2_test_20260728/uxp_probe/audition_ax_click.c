#include <ApplicationServices/ApplicationServices.h>
#include <CoreFoundation/CoreFoundation.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

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
    AXUIElementRef result = NULL;
    CFIndex count = CFArrayGetCount(children);
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

int main(int argc, char **argv) {
    if (argc != 4) {
        fprintf(stderr, "Uso: audition_ax_click PID PAPEL ROTULO\n");
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
    CGPoint position = CGPointZero;
    CGSize size = CGSizeZero;
    if (!element || !copy_geometry(element, &position, &size)) {
        fprintf(stderr, "Elemento ou geometria não encontrado.\n");
        if (element) CFRelease(element);
        CFRelease(app);
        CFRelease(label);
        CFRelease(role);
        return 1;
    }

    CGPoint center = CGPointMake(
        position.x + size.width / 2.0,
        position.y + size.height / 2.0
    );
    CGEventRef down = CGEventCreateMouseEvent(
        NULL,
        kCGEventLeftMouseDown,
        center,
        kCGMouseButtonLeft
    );
    CGEventRef up = CGEventCreateMouseEvent(
        NULL,
        kCGEventLeftMouseUp,
        center,
        kCGMouseButtonLeft
    );
    CGEventPostToPid(pid, down);
    usleep(60000);
    CGEventPostToPid(pid, up);

    printf(
        "{\"pid\":%d,\"visited\":%d,\"x\":%.0f,\"y\":%.0f}\n",
        pid,
        visited,
        center.x,
        center.y
    );

    CFRelease(up);
    CFRelease(down);
    CFRelease(element);
    CFRelease(app);
    CFRelease(label);
    CFRelease(role);
    return 0;
}

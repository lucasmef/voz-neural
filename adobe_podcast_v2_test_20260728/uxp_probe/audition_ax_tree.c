#include <ApplicationServices/ApplicationServices.h>
#include <CoreFoundation/CoreFoundation.h>
#include <stdio.h>
#include <stdlib.h>

static void print_indent(int depth) {
    for (int index = 0; index < depth; ++index) fputs("  ", stdout);
}

static void print_string_attribute(
    AXUIElementRef element,
    CFStringRef attribute,
    const char *label
) {
    CFTypeRef value = NULL;
    if (AXUIElementCopyAttributeValue(element, attribute, &value) !=
            kAXErrorSuccess ||
        !value ||
        CFGetTypeID(value) != CFStringGetTypeID()) {
        if (value) CFRelease(value);
        return;
    }

    CFIndex length = CFStringGetLength((CFStringRef)value);
    CFIndex capacity = CFStringGetMaximumSizeForEncoding(
        length,
        kCFStringEncodingUTF8
    ) + 1;
    char *buffer = calloc((size_t)capacity, sizeof(char));
    if (buffer &&
        CFStringGetCString(
            (CFStringRef)value,
            buffer,
            capacity,
            kCFStringEncodingUTF8) &&
        buffer[0] != '\0') {
        printf(" %s=\"", label);
        for (const char *cursor = buffer; *cursor; ++cursor) {
            if (*cursor == '"' || *cursor == '\\') putchar('\\');
            if (*cursor == '\n' || *cursor == '\r') {
                fputs("\\n", stdout);
            } else {
                putchar(*cursor);
            }
        }
        putchar('"');
    }
    free(buffer);
    CFRelease(value);
}

static void print_point_attribute(
    AXUIElementRef element,
    CFStringRef attribute,
    const char *label
) {
    CFTypeRef value = NULL;
    CGPoint point = CGPointZero;
    if (AXUIElementCopyAttributeValue(element, attribute, &value) ==
            kAXErrorSuccess &&
        value &&
        CFGetTypeID(value) == AXValueGetTypeID() &&
        AXValueGetValue((AXValueRef)value, kAXValueCGPointType, &point)) {
        printf(" %s=(%.0f,%.0f)", label, point.x, point.y);
    }
    if (value) CFRelease(value);
}

static void print_size_attribute(
    AXUIElementRef element,
    CFStringRef attribute,
    const char *label
) {
    CFTypeRef value = NULL;
    CGSize size = CGSizeZero;
    if (AXUIElementCopyAttributeValue(element, attribute, &value) ==
            kAXErrorSuccess &&
        value &&
        CFGetTypeID(value) == AXValueGetTypeID() &&
        AXValueGetValue((AXValueRef)value, kAXValueCGSizeType, &size)) {
        printf(" %s=(%.0f,%.0f)", label, size.width, size.height);
    }
    if (value) CFRelease(value);
}

static void walk(AXUIElementRef element, int depth, int *count) {
    if (depth > 18 || *count > 3000) return;
    *count += 1;

    print_indent(depth);
    printf("[%d]", *count);
    print_string_attribute(element, kAXRoleAttribute, "role");
    print_string_attribute(element, kAXSubroleAttribute, "subrole");
    print_string_attribute(element, kAXTitleAttribute, "title");
    print_string_attribute(element, kAXDescriptionAttribute, "description");
    print_string_attribute(element, kAXValueAttribute, "value");
    print_string_attribute(element, kAXIdentifierAttribute, "identifier");
    print_point_attribute(element, kAXPositionAttribute, "position");
    print_size_attribute(element, kAXSizeAttribute, "size");
    putchar('\n');

    CFTypeRef childrenValue = NULL;
    if (AXUIElementCopyAttributeValue(
            element,
            kAXChildrenAttribute,
            &childrenValue) != kAXErrorSuccess ||
        !childrenValue ||
        CFGetTypeID(childrenValue) != CFArrayGetTypeID()) {
        if (childrenValue) CFRelease(childrenValue);
        return;
    }

    CFArrayRef children = (CFArrayRef)childrenValue;
    CFIndex childCount = CFArrayGetCount(children);
    for (CFIndex index = 0; index < childCount; ++index) {
        walk(
            (AXUIElementRef)CFArrayGetValueAtIndex(children, index),
            depth + 1,
            count
        );
    }
    CFRelease(childrenValue);
}

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "Uso: audition_ax_tree PID\n");
        return 2;
    }

    pid_t pid = (pid_t)strtol(argv[1], NULL, 10);
    if (pid <= 0) {
        fprintf(stderr, "PID inválido.\n");
        return 2;
    }

    AXUIElementRef app = AXUIElementCreateApplication(pid);
    int count = 0;
    walk(app, 0, &count);
    CFRelease(app);
    return 0;
}

#include <ApplicationServices/ApplicationServices.h>
#include <CoreFoundation/CoreFoundation.h>
#include <stdio.h>
#include <stdlib.h>

static long long dictionary_number(
    CFDictionaryRef dictionary,
    const void *key
) {
    CFNumberRef number = CFDictionaryGetValue(dictionary, key);
    long long value = 0;
    if (number) {
        CFNumberGetValue(number, kCFNumberLongLongType, &value);
    }
    return value;
}

static void print_string(CFDictionaryRef dictionary, const void *key) {
    CFStringRef string = CFDictionaryGetValue(dictionary, key);
    char buffer[2048] = {0};
    if (string && CFStringGetCString(
            string,
            buffer,
            sizeof(buffer),
            kCFStringEncodingUTF8)) {
        printf("%s", buffer);
    }
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "Uso: list_audition_windows PID [PID...]\n");
        return 2;
    }

    CFArrayRef windows = CGWindowListCopyWindowInfo(
        kCGWindowListOptionAll,
        kCGNullWindowID
    );
    if (!windows) return 1;

    CFIndex count = CFArrayGetCount(windows);
    for (CFIndex index = 0; index < count; ++index) {
        CFDictionaryRef window = CFArrayGetValueAtIndex(windows, index);
        long long ownerPid = dictionary_number(window, kCGWindowOwnerPID);
        int match = 0;
        for (int argument = 1; argument < argc; ++argument) {
            if (ownerPid == strtoll(argv[argument], NULL, 10)) {
                match = 1;
                break;
            }
        }
        if (!match) continue;

        long long number = dictionary_number(window, kCGWindowNumber);
        long long layer = dictionary_number(window, kCGWindowLayer);
        long long alphaMilli = 0;
        CFNumberRef alpha = CFDictionaryGetValue(window, kCGWindowAlpha);
        double alphaValue = 0;
        if (alpha) CFNumberGetValue(alpha, kCFNumberDoubleType, &alphaValue);
        alphaMilli = (long long)(alphaValue * 1000.0);

        CGRect bounds = CGRectZero;
        CFDictionaryRef boundsDictionary =
            CFDictionaryGetValue(window, kCGWindowBounds);
        if (boundsDictionary) {
            CGRectMakeWithDictionaryRepresentation(boundsDictionary, &bounds);
        }

        printf(
            "pid=%lld window=%lld layer=%lld alpha=%lld "
            "x=%.0f y=%.0f w=%.0f h=%.0f owner=",
            ownerPid,
            number,
            layer,
            alphaMilli,
            bounds.origin.x,
            bounds.origin.y,
            bounds.size.width,
            bounds.size.height
        );
        print_string(window, kCGWindowOwnerName);
        printf(" title=");
        print_string(window, kCGWindowName);
        printf("\n");
    }

    CFRelease(windows);
    return 0;
}

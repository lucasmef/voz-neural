#import <AVFoundation/AVFoundation.h>
#import <Foundation/Foundation.h>

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc != 3) {
            fprintf(stderr, "usage: extract_audio_m4a INPUT OUTPUT\n");
            return 2;
        }

        NSString *inputPath = [NSString stringWithUTF8String:argv[1]];
        NSString *outputPath = [NSString stringWithUTF8String:argv[2]];
        NSURL *inputURL = [NSURL fileURLWithPath:inputPath];
        NSURL *outputURL = [NSURL fileURLWithPath:outputPath];
        AVURLAsset *asset = [AVURLAsset URLAssetWithURL:inputURL options:nil];
        AVAssetExportSession *session =
            [[AVAssetExportSession alloc] initWithAsset:asset
                                             presetName:AVAssetExportPresetAppleM4A];
        if (session == nil) {
            fprintf(stderr, "could not create AVAssetExportSession\n");
            return 3;
        }

        session.outputURL = outputURL;
        session.outputFileType = AVFileTypeAppleM4A;
        session.shouldOptimizeForNetworkUse = NO;

        dispatch_semaphore_t semaphore = dispatch_semaphore_create(0);
        [session exportAsynchronouslyWithCompletionHandler:^{
            dispatch_semaphore_signal(semaphore);
        }];
        dispatch_semaphore_wait(semaphore, DISPATCH_TIME_FOREVER);

        if (session.status != AVAssetExportSessionStatusCompleted) {
            NSString *message = session.error.localizedDescription ?: @"unknown error";
            fprintf(stderr, "export failed: %s\n", message.UTF8String);
            return 4;
        }

        printf("%s\n", outputPath.UTF8String);
        return 0;
    }
}

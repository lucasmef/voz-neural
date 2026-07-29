#import <AVFoundation/AVFoundation.h>
#import <Foundation/Foundation.h>

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc != 3) {
            fprintf(stderr, "usage: extract_audio_wav INPUT OUTPUT\n");
            return 2;
        }

        NSString *inputPath = [NSString stringWithUTF8String:argv[1]];
        NSString *outputPath = [NSString stringWithUTF8String:argv[2]];
        AVURLAsset *asset = [AVURLAsset URLAssetWithURL:[NSURL fileURLWithPath:inputPath]
                                               options:nil];
        AVAssetTrack *track = [[asset tracksWithMediaType:AVMediaTypeAudio] firstObject];
        if (track == nil) {
            fprintf(stderr, "no audio track\n");
            return 3;
        }

        NSError *error = nil;
        AVAssetReader *reader = [[AVAssetReader alloc] initWithAsset:asset error:&error];
        if (reader == nil) {
            fprintf(stderr, "reader failed: %s\n", error.localizedDescription.UTF8String);
            return 4;
        }

        NSDictionary *settings = @{
            AVFormatIDKey: @(kAudioFormatLinearPCM),
            AVSampleRateKey: @44100,
            AVNumberOfChannelsKey: @2,
            AVLinearPCMBitDepthKey: @16,
            AVLinearPCMIsBigEndianKey: @NO,
            AVLinearPCMIsFloatKey: @NO,
            AVLinearPCMIsNonInterleaved: @NO
        };

        AVAssetReaderTrackOutput *readerOutput =
            [[AVAssetReaderTrackOutput alloc] initWithTrack:track outputSettings:settings];
        readerOutput.alwaysCopiesSampleData = NO;
        if (![reader canAddOutput:readerOutput]) {
            fprintf(stderr, "cannot add reader output\n");
            return 5;
        }
        [reader addOutput:readerOutput];

        NSURL *outputURL = [NSURL fileURLWithPath:outputPath];
        AVAssetWriter *writer =
            [[AVAssetWriter alloc] initWithURL:outputURL fileType:AVFileTypeWAVE error:&error];
        if (writer == nil) {
            fprintf(stderr, "writer failed: %s\n", error.localizedDescription.UTF8String);
            return 6;
        }

        AVAssetWriterInput *writerInput =
            [[AVAssetWriterInput alloc] initWithMediaType:AVMediaTypeAudio
                                          outputSettings:settings];
        writerInput.expectsMediaDataInRealTime = NO;
        if (![writer canAddInput:writerInput]) {
            fprintf(stderr, "cannot add writer input\n");
            return 7;
        }
        [writer addInput:writerInput];

        if (![writer startWriting]) {
            fprintf(stderr, "writer start failed: %s\n", writer.error.localizedDescription.UTF8String);
            return 8;
        }
        [writer startSessionAtSourceTime:kCMTimeZero];
        if (![reader startReading]) {
            fprintf(stderr, "reader start failed: %s\n", reader.error.localizedDescription.UTF8String);
            return 9;
        }

        while (reader.status == AVAssetReaderStatusReading) {
            if (!writerInput.readyForMoreMediaData) {
                usleep(1000);
                continue;
            }
            CMSampleBufferRef sample = [readerOutput copyNextSampleBuffer];
            if (sample == NULL) {
                break;
            }
            BOOL appended = [writerInput appendSampleBuffer:sample];
            CFRelease(sample);
            if (!appended) {
                fprintf(stderr, "append failed: %s\n", writer.error.localizedDescription.UTF8String);
                [reader cancelReading];
                return 10;
            }
        }

        if (reader.status == AVAssetReaderStatusFailed) {
            fprintf(stderr, "reader failed: %s\n", reader.error.localizedDescription.UTF8String);
            [writer cancelWriting];
            return 11;
        }

        [writerInput markAsFinished];
        dispatch_semaphore_t semaphore = dispatch_semaphore_create(0);
        [writer finishWritingWithCompletionHandler:^{
            dispatch_semaphore_signal(semaphore);
        }];
        dispatch_semaphore_wait(semaphore, DISPATCH_TIME_FOREVER);

        if (writer.status != AVAssetWriterStatusCompleted) {
            fprintf(stderr, "writer finish failed: %s\n", writer.error.localizedDescription.UTF8String);
            return 12;
        }

        printf("%s\n", outputPath.UTF8String);
        return 0;
    }
}

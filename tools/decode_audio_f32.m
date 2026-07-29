#import <AVFoundation/AVFoundation.h>
#import <Foundation/Foundation.h>

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc != 3) {
            fprintf(stderr, "usage: decode_audio_f32 INPUT OUTPUT_F32\n");
            return 2;
        }

        NSString *inputPath = [NSString stringWithUTF8String:argv[1]];
        NSString *outputPath = [NSString stringWithUTF8String:argv[2]];
        NSError *error = nil;
        AVAudioFile *audio =
            [[AVAudioFile alloc] initForReading:[NSURL fileURLWithPath:inputPath]
                                         error:&error];
        if (audio == nil) {
            fprintf(stderr, "open failed: %s\n", error.localizedDescription.UTF8String);
            return 3;
        }

        AVAudioFormat *format = audio.processingFormat;
        fprintf(stderr, "file format: %s\n", audio.fileFormat.description.UTF8String);
        fprintf(stderr, "processing format: %s\n", format.description.UTF8String);
        if (format.commonFormat != AVAudioPCMFormatFloat32 || format.isInterleaved) {
            fprintf(stderr, "unexpected processing format: %s\n",
                    format.description.UTF8String);
            return 4;
        }

        FILE *output = fopen(outputPath.UTF8String, "wb");
        if (output == NULL) {
            perror("fopen");
            return 5;
        }

        AVAudioFrameCount capacity = 8192;
        AVAudioPCMBuffer *buffer =
            [[AVAudioPCMBuffer alloc] initWithPCMFormat:format frameCapacity:capacity];
        uint64_t totalFrames = 0;
        while (true) {
            error = nil;
            BOOL ok = [audio readIntoBuffer:buffer
                                 frameCount:capacity
                                      error:&error];
            if (!ok) {
                if (error == nil) {
                    break;
                }
                fprintf(stderr, "read failed: %s\n", error.localizedDescription.UTF8String);
                fclose(output);
                return 6;
            }
            AVAudioFrameCount frames = buffer.frameLength;
            if (frames == 0) {
                break;
            }

            float *const *channels = buffer.floatChannelData;
            AVAudioChannelCount channelCount = format.channelCount;
            for (AVAudioFrameCount frame = 0; frame < frames; frame++) {
                float mono = 0.0f;
                for (AVAudioChannelCount channel = 0; channel < channelCount; channel++) {
                    mono += channels[channel][frame];
                }
                mono /= (float)channelCount;
                fwrite(&mono, sizeof(float), 1, output);
            }
            totalFrames += frames;
        }
        fclose(output);

        printf(
            "{\"sample_rate\":%.6f,\"channels\":%u,\"frames\":%llu,"
            "\"duration\":%.9f,\"source\":\"%s\",\"decoded\":\"%s\"}\n",
            format.sampleRate,
            (unsigned int)format.channelCount,
            totalFrames,
            totalFrames / format.sampleRate,
            inputPath.UTF8String,
            outputPath.UTF8String
        );
        return 0;
    }
}

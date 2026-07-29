#import <CoreML/CoreML.h>
#import <Foundation/Foundation.h>

static void DescribeModel(MLModel *model, NSString *label) {
    MLModelDescription *description = model.modelDescription;
    NSLog(@"%@: loaded", label);
    NSLog(@"inputs: %@", description.inputDescriptionsByName);
    NSLog(@"outputs: %@", description.outputDescriptionsByName);
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc != 2) {
            fprintf(stderr, "usage: CoreMLProbe <model-path>\n");
            return 2;
        }

        NSString *path = [NSString stringWithUTF8String:argv[1]];
        NSURL *sourceURL = [NSURL fileURLWithPath:path];
        NSError *error = nil;

        MLModel *model = [MLModel modelWithContentsOfURL:sourceURL error:&error];
        if (model != nil) {
            DescribeModel(model, @"direct");
            return 0;
        }
        NSLog(@"direct load failed: %@", error);

        error = nil;
        NSURL *compiledURL = [MLModel compileModelAtURL:sourceURL error:&error];
        if (compiledURL == nil) {
            NSLog(@"compile failed: %@", error);
            return 1;
        }

        NSLog(@"compiled at: %@", compiledURL.path);
        error = nil;
        model = [MLModel modelWithContentsOfURL:compiledURL error:&error];
        if (model == nil) {
            NSLog(@"compiled load failed: %@", error);
            return 1;
        }

        DescribeModel(model, @"compiled");
        return 0;
    }
}

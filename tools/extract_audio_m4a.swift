import AVFoundation
import Foundation

guard CommandLine.arguments.count == 3 else {
    fputs("usage: extract_audio_m4a.swift INPUT OUTPUT\n", stderr)
    exit(2)
}

let inputURL = URL(fileURLWithPath: CommandLine.arguments[1])
let outputURL = URL(fileURLWithPath: CommandLine.arguments[2])
let asset = AVURLAsset(url: inputURL)

guard let session = AVAssetExportSession(
    asset: asset,
    presetName: AVAssetExportPresetAppleM4A
) else {
    fputs("could not create AVAssetExportSession\n", stderr)
    exit(3)
}

session.outputURL = outputURL
session.outputFileType = .m4a
session.shouldOptimizeForNetworkUse = false

let semaphore = DispatchSemaphore(value: 0)
session.exportAsynchronously {
    semaphore.signal()
}
semaphore.wait()

switch session.status {
case .completed:
    print(outputURL.path)
case .failed, .cancelled:
    fputs("export failed: \(session.error?.localizedDescription ?? "unknown error")\n", stderr)
    exit(4)
default:
    fputs("export ended with unexpected status: \(session.status.rawValue)\n", stderr)
    exit(5)
}

import AVFoundation
import AudioToolbox
import Foundation

func fourCC(_ value: AudioFormatID) -> String {
    let bytes: [UInt8] = [
        UInt8((value >> 24) & 0xff),
        UInt8((value >> 16) & 0xff),
        UInt8((value >> 8) & 0xff),
        UInt8(value & 0xff),
    ]
    return String(bytes: bytes, encoding: .macOSRoman) ?? String(value)
}

@main
struct AudioInspector {
    static func main() async {
        guard CommandLine.arguments.count == 2 else {
            fputs("usage: inspect_audio INPUT\n", stderr)
            exit(2)
        }
        do {
            let url = URL(fileURLWithPath: CommandLine.arguments[1])
            let asset = AVURLAsset(url: url)
            let duration = try await asset.load(.duration)
            let tracks = try await asset.loadTracks(withMediaType: .audio)
            guard let track = tracks.first else {
                fputs("no audio track\n", stderr)
                exit(1)
            }
            let descriptions = try await track.load(.formatDescriptions)
            guard let description = descriptions.first,
                  let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(description)?.pointee else {
                fputs("no ASBD\n", stderr)
                exit(1)
            }
            let trackDuration = try await track.load(.timeRange).duration
            let payload: [String: Any] = [
                "path": url.path,
                "asset_duration_seconds": CMTimeGetSeconds(duration),
                "track_duration_seconds": CMTimeGetSeconds(trackDuration),
                "sample_rate": asbd.mSampleRate,
                "channels": asbd.mChannelsPerFrame,
                "format_id": fourCC(asbd.mFormatID),
                "format_flags": asbd.mFormatFlags,
                "bits_per_channel": asbd.mBitsPerChannel,
                "bytes_per_frame": asbd.mBytesPerFrame,
                "frames_per_packet": asbd.mFramesPerPacket,
            ]
            let data = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write(Data("\n".utf8))
        } catch {
            fputs("inspect_audio: \(error)\n", stderr)
            exit(1)
        }
    }
}

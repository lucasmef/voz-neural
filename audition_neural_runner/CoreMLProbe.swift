import CoreML
import Foundation

guard CommandLine.arguments.count == 2 else {
    fputs("usage: CoreMLProbe <model-path>\n", stderr)
    exit(2)
}

let sourceURL = URL(fileURLWithPath: CommandLine.arguments[1])
let manager = FileManager.default

guard manager.fileExists(atPath: sourceURL.path) else {
    fputs("model not found: \(sourceURL.path)\n", stderr)
    exit(2)
}

func describe(_ model: MLModel, label: String) {
    let description = model.modelDescription
    print("\(label): loaded")
    print("inputs:")
    for (name, feature) in description.inputDescriptionsByName.sorted(by: { $0.key < $1.key }) {
        print("  \(name): \(feature.type.rawValue) \(feature)")
    }
    print("outputs:")
    for (name, feature) in description.outputDescriptionsByName.sorted(by: { $0.key < $1.key }) {
        print("  \(name): \(feature.type.rawValue) \(feature)")
    }
}

do {
    let model = try MLModel(contentsOf: sourceURL)
    describe(model, label: "direct")
    exit(0)
} catch {
    print("direct load failed: \(error)")
}

do {
    let compiledURL = try MLModel.compileModel(at: sourceURL)
    print("compiled at: \(compiledURL.path)")
    let model = try MLModel(contentsOf: compiledURL)
    describe(model, label: "compiled")
    exit(0)
} catch {
    print("compile/load failed: \(error)")
    exit(1)
}

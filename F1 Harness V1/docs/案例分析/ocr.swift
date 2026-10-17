import Foundation
import Vision
import AppKit

// 用法: swift ocr.swift <帧目录>  → 每行输出 "文件名\t字幕文本"
let dir = CommandLine.arguments[1]
let files = try FileManager.default.contentsOfDirectory(atPath: dir).filter { $0.hasSuffix(".png") }.sorted()
for f in files {
    guard let img = NSImage(contentsOfFile: dir + "/" + f),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { continue }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["zh-Hans", "en-US"]
    req.usesLanguageCorrection = true
    try? VNImageRequestHandler(cgImage: cg).perform([req])
    // 取最高的一行（字幕字号最大），并要求水平居中
    let obs = (req.results ?? []).filter { abs($0.boundingBox.midX - 0.5) < 0.2 && $0.boundingBox.height > 0.18 }
    let best = obs.max { $0.boundingBox.height < $1.boundingBox.height }
    let text = best?.topCandidates(1).first?.string ?? ""
    print("\(f)\t\(text)")
}

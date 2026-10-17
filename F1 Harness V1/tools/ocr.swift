// 本机文字识别（macOS Vision）：从标准输入逐行读图片路径，每张图输出一行 JSON。
// 坐标归一化到 0–1，原点在左上角。编译：swiftc -O -o tools/bin/hermes-ocr tools/ocr.swift
import Foundation
import Vision
import ImageIO

func emit(_ obj: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: obj, options: [])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

let fast = CommandLine.arguments.contains("--fast")

while let line = readLine() {
    let path = line.trimmingCharacters(in: .whitespacesAndNewlines)
    if path.isEmpty { continue }
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
        emit(["path": path, "error": "无法读取图片"])
        continue
    }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = fast ? .fast : .accurate
    req.recognitionLanguages = ["zh-Hans", "en-US"]
    // 车手缩写、圈数不是自然语言，关掉纠错免得被改成单词
    req.usesLanguageCorrection = false
    do {
        try VNImageRequestHandler(cgImage: img, options: [:]).perform([req])
    } catch {
        emit(["path": path, "error": "\(error)"])
        continue
    }
    var lines: [[String: Any]] = []
    for obs in req.results ?? [] {
        guard let cand = obs.topCandidates(1).first else { continue }
        let b = obs.boundingBox
        lines.append(["t": cand.string, "c": Double(cand.confidence),
                      "x": Double(b.minX), "y": Double(1 - b.maxY), "w": Double(b.width), "h": Double(b.height)])
    }
    emit(["path": path, "w": img.width, "h": img.height, "lines": lines])
}

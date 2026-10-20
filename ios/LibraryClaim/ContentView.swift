import SwiftUI
import RoomPlan
import ARKit

private enum ClaimPalette {
    static let ivory = Color(red: 247 / 255, green: 243 / 255, blue: 232 / 255)
    static let forest = Color(red: 18 / 255, green: 60 / 255, blue: 50 / 255)
    static let gold = Color(red: 216 / 255, green: 184 / 255, blue: 107 / 255)
    static let ink = Color(red: 30 / 255, green: 48 / 255, blue: 41 / 255)
    static let secondary = Color(red: 91 / 255, green: 105 / 255, blue: 94 / 255)
    static let line = Color(red: 224 / 255, green: 226 / 255, blue: 214 / 255)
    static let sage = Color(red: 235 / 255, green: 240 / 255, blue: 229 / 255)
}

struct ContentView: View {
    @StateObject private var model = CaptureController()
    @Environment(\.scenePhase) private var scenePhase
    @State private var selected: InventorySelection?
    @State private var showSavedInventories = false
    @State private var startPressed = false
    @State private var isStartingVoice = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    brandHeader
                    if startPressed || model.hasConversation {
                        Button { showSavedInventories = true } label: {
                            Label("Saved inventories",systemImage:"tray.full")
                                .frame(maxWidth:.infinity,minHeight:44)
                        }
                        .buttonStyle(ClaimSecondaryButtonStyle())
                    }
                    if model.cameraView != nil {
                        ObjectCameraPanel(overlays: model.overlays, isCapturing: model.isCapturing)
                    }
                    if startPressed || model.hasConversation { assistantPanel }
                    else { introduction }
                    captureActions
                    if model.hasCapture { captureResults }
                    if model.comparison != .null { comparisonPanel }
                    if model.secondLocale != .null { additionalMarketPanel }
                    if model.canStartAgain {
                        Button { Task { await model.startAgain() } } label: {
                            Label("Start again", systemImage: "arrow.counterclockwise")
                                .frame(maxWidth: .infinity, minHeight: 44)
                        }
                        .buttonStyle(ClaimSecondaryButtonStyle())
                    }
                }
                .padding(.horizontal, 20)
                .padding(.top, 18)
                .padding(.bottom, 28)
            }
            .background(ClaimPalette.ivory)
            .safeAreaInset(edge: .bottom, spacing: 0) { voiceDock }
            .toolbar(.hidden, for: .navigationBar)
            .task {
                await model.prepareCameraPreview()
                #if DEBUG
                if CommandLine.arguments.contains("--voice-diagnostics") {
                    startPressed = true
                    await model.start()
                }
                #endif
            }
            .sheet(item: $selected) { selection in
                InventoryDetailSheet(model:model,selection:selection) { selected = nil }
            }
            .sheet(isPresented:$showSavedInventories) {
                SavedInventoriesSheet(capture:model)
            }
            .onChange(of: scenePhase) { _, value in
                if value == .background { model.interrupted() }
                else if value == .active { model.foregrounded() }
            }
        }
        .tint(ClaimPalette.forest)
        .foregroundStyle(ClaimPalette.ink)
        .preferredColorScheme(.light)
    }

    private var brandHeader: some View {
        HStack(alignment: .center, spacing: 12) {
            Image("LibraryClaimLogo")
                .resizable()
                .scaledToFit()
                .frame(width: 52, height: 52)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text("Library Claim")
                    .font(.system(.title2, design: .serif).weight(.semibold))
                    .foregroundStyle(ClaimPalette.forest)
                Text("A record of what matters.")
                    .font(.caption)
                    .foregroundStyle(ClaimPalette.secondary)
            }
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .combine)
    }

    private var introduction: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("One room.\nOne conversation.")
                .font(.system(.largeTitle, design: .serif).weight(.medium))
                .foregroundStyle(ClaimPalette.forest)
                .fixedSize(horizontal: false, vertical: true)
            Text("Tell the assistant your language and pricing country. This iPhone uses LiDAR when supported, with camera fallback otherwise.")
                .font(.subheadline)
                .foregroundStyle(ClaimPalette.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var assistantPanel: some View {
        VStack(alignment: .leading, spacing: 16) {
            ClaimSectionHeading("Your assistant", symbol: "waveform")
            Text(model.status)
                .font(.headline)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("assistant-status")
            Text(model.localVoiceStatus)
                .font(.caption)
                .foregroundStyle(ClaimPalette.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "sparkle")
                    .foregroundStyle(ClaimPalette.forest)
                    .accessibilityHidden(true)
                Text(model.guidance)
                    .font(.callout)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(ClaimPalette.sage, in: RoundedRectangle(cornerRadius: 16))

            if !model.cameraSupported {
                Label("Conversation is available here. Room capture requires a supported physical iPhone.", systemImage: "iphone")
                    .font(.caption)
                    .foregroundStyle(ClaimPalette.secondary)
            }
            if !model.transcript.isEmpty {
                DisclosureGroup {
                    Text(model.transcript)
                        .font(.callout)
                        .foregroundStyle(ClaimPalette.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.top, 10)
                        .textSelection(.enabled)
                } label: {
                    Label("Conversation transcript", systemImage: "text.bubble")
                        .font(.subheadline.weight(.medium))
                        .padding(.vertical, 5)
                }
            }
            DisclosureGroup {
                VStack(alignment: .leading, spacing: 10) {
                    Text(model.intelligenceStatus)
                        .accessibilityIdentifier("device-intelligence-status")
                    if model.hasConversation {
                        Text(model.isVoiceConnected ? "Assistant voice connected" : "Voice stopped or connecting")
                    }
                }
                .font(.caption)
                .foregroundStyle(ClaimPalette.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, 10)
            } label: {
                Label("Connection & device details", systemImage: "info.circle")
                    .font(.subheadline)
                    .padding(.vertical, 5)
            }
        }
        .claimCard()
    }

    private var voiceDock: some View {
        VStack(spacing: 8) {
            if isStartingVoice || model.isStarting || model.isVoiceStarting {
                ProgressView("Connecting…")
                    .tint(ClaimPalette.forest)
                    .frame(maxWidth:.infinity,minHeight:48)
            } else if !model.hasConversation {
                Button(action:startConversation) {
                    ClaimActionLabel(title:startPressed ? "Try again" : "Start", symbol:"mic.fill")
                }
                .buttonStyle(ClaimPrimaryButtonStyle())
            } else {
                if model.isVoiceConnected {
                    Button { model.interruptAndListen() } label: {
                        ClaimActionLabel(title: "Interrupt & speak", symbol: "mic.fill")
                    }
                    .buttonStyle(ClaimPrimaryButtonStyle())
                    HStack {
                        Button { Task { await model.stopVoice() } } label: {
                            Label("Stop voice",systemImage:"stop.fill")
                                .frame(maxWidth:.infinity,minHeight:44)
                        }
                        Button { Task { await model.connectVoice() } } label: {
                            Label("Reconnect", systemImage: "arrow.clockwise")
                                .frame(maxWidth: .infinity, minHeight: 44)
                        }
                        .disabled(model.isStarting || model.canRetryHandoff)
                    }
                    .font(.subheadline.weight(.medium))
                } else {
                    Button { Task { await model.connectVoice() } } label: {
                        ClaimActionLabel(title: model.isStarting ? "Connecting…" : "Reconnect voice", symbol: "mic.fill", isLoading: model.isStarting)
                    }
                    .buttonStyle(ClaimPrimaryButtonStyle())
                    .disabled(model.isStarting || model.canRetryHandoff)
                    Button { Task { await model.stopVoice() } } label: {
                        Text("Stop voice")
                            .font(.subheadline.weight(.medium))
                            .frame(maxWidth: .infinity, minHeight: 44)
                    }
                }
            }
            Text("AI-generated voice · Audio sent to OpenAI")
                .font(.caption2)
                .foregroundStyle(ClaimPalette.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(.horizontal, 20)
        .padding(.top, 14)
        .padding(.bottom, 8)
        .background(ClaimPalette.ivory)
        .overlay(alignment: .top) { Rectangle().fill(ClaimPalette.line).frame(height: 1) }
    }

    private func startConversation() {
        // Hide Start synchronously, before permission or network work can suspend.
        startPressed = true
        isStartingVoice = true
        Task {
            await model.start()
            isStartingVoice = false
        }
    }

    @ViewBuilder private var captureActions: some View {
        if model.hasCapture || model.isFinishing || model.canRetryHandoff || model.canRetryFinish {
            VStack(alignment: .leading, spacing: 14) {
                if model.hasCapture {
                    HStack(alignment: .firstTextBaseline) {
                        Label("Room sweep", systemImage: "viewfinder")
                            .font(.subheadline.weight(.semibold))
                        Spacer(minLength: 8)
                        Text(String(format: "%02d:%02d", Int(model.elapsed) / 60, Int(model.elapsed) % 60))
                            .font(.system(.title2, design: .rounded).weight(.medium))
                            .monospacedDigit()
                    }
                    Text("\(model.savedFrames) frames · \(model.pendingUploads) uploading")
                        .font(.caption)
                        .foregroundStyle(ClaimPalette.secondary)
                }
                if model.isCapturing {
                    Button { model.endCapture() } label: {
                        ClaimActionLabel(title: "End sweep & build packet", symbol: "checkmark.circle")
                    }
                    .buttonStyle(ClaimPrimaryButtonStyle())
                }
                if model.isFinishing {
                    HStack(alignment: .top, spacing: 12) {
                        ProgressView().tint(ClaimPalette.forest)
                        Text("Keep the app open while evidence uploads and processing finishes.")
                            .font(.callout)
                    }
                }
                if model.canRetryHandoff {
                    Button { Task { await model.retryCaptureHandoff() } } label: {
                        ClaimActionLabel(title: "Retry capture handoff", symbol: "arrow.clockwise")
                    }
                    .buttonStyle(ClaimPrimaryButtonStyle())
                    .disabled(model.isStarting)
                }
                if model.canRetryFinish {
                    Button { Task { await model.finalize() } } label: {
                        ClaimActionLabel(title: "Retry finish", symbol: "arrow.clockwise")
                    }
                    .buttonStyle(ClaimPrimaryButtonStyle())
                }
            }
            .claimCard()
        }
    }

    private var captureResults: some View {
        VStack(alignment: .leading, spacing: 24) {
            RoomMeasurementsPanel(room:model.packet["room"],mode:model.captureMode,isCapturing:model.isCapturing,
                detectedFloorCount:model.detectedFloorCount,detectedWallCount:model.detectedWallCount,
                scanStatus:model.roomScanStatus,coaching:model.roomScanCoaching)
            inventoryPanel
            if model.packetURL != nil || model.reviewURL != nil { packetActions }
        }
    }

    private var inventoryPanel: some View {
        VStack(alignment: .leading, spacing: 18) {
            ClaimSectionHeading("Your inventory", symbol: "books.vertical")
            ViewThatFits(in: .horizontal) {
                HStack(alignment: .top, spacing: 12) {
                    metric("Books", String(model.count))
                    metric("To review", String(model.reviewCount))
                    metric("Currency", model.currency)
                }
                VStack(alignment: .leading, spacing: 16) {
                    metric("Books", String(model.count))
                    metric("To review", String(model.reviewCount))
                    metric("Currency", model.currency)
                }
            }
            if !model.rows.isEmpty {
                VStack(spacing: 0) {
                    ForEach(model.rows) { row in
                        InventoryListRow(row:row) { selected = InventorySelection(id:row.id) }
                        if row.id != model.rows.last?.id {
                            Divider().overlay(ClaimPalette.line)
                        }
                    }
                }
            }
            DisclosureGroup {
                VStack(alignment: .leading, spacing: 10) {
                    if let cost = model.packet["metrics"]["cost_usd"].number {
                        Text(String(format: "API usage: $%.4f USD · %@", cost, model.packet["metrics"]["cost_status"].string ?? "estimated"))
                    } else {
                        Text("API usage cost unavailable")
                    }
                    Text("\(Int(model.packet["metrics"]["duplicate_images_suppressed"].number ?? 0)) repeat photos skipped on server")
                }
                .font(.caption)
                .foregroundStyle(ClaimPalette.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, 10)
            } label: {
                Text("Processing details").font(.subheadline)
                    .padding(.vertical, 5)
            }
        }
        .claimCard()
    }

    private var packetActions: some View {
        VStack(alignment: .leading, spacing: 14) {
            ClaimSectionHeading("Your claim packet", symbol: "doc.text")
            if let url = model.packetURL {
                ShareLink(item: url) {
                    ClaimActionLabel(title: "Save or share claim packet", symbol: "square.and.arrow.up")
                }
                .buttonStyle(ClaimPrimaryButtonStyle())
            }
            if let url = model.reviewURL {
                Link(destination: url) {
                    Label("Open full review on this phone", systemImage: "arrow.up.right")
                        .font(.subheadline.weight(.medium))
                        .frame(maxWidth: .infinity, minHeight: 44)
                }
            }
        }
        .claimCard()
    }

    private var comparisonPanel: some View {
        VStack(alignment: .leading, spacing: 16) {
            ClaimSectionHeading("Camera & LiDAR", symbol: "square.split.2x2")
            Text("Response differences from the same images. Accuracy requires independent ground truth.")
                .font(.caption)
                .foregroundStyle(ClaimPalette.secondary)
            if let reason = model.comparison["reason"].string {
                Text(reason).font(.callout)
            } else {
                ScrollView(.horizontal) {
                    comparisonGrid
                        .padding(.bottom, 4)
                }
            }
            DisclosureGroup {
                Text(model.comparison.pretty)
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(ClaimPalette.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, 10)
                    .textSelection(.enabled)
            } label: {
                Text("Response summaries and metrics")
                    .font(.subheadline)
                    .padding(.vertical, 5)
            }
        }
        .claimCard()
    }

    private var comparisonGrid: some View {
        Grid(alignment: .leading, horizontalSpacing: 20, verticalSpacing: 14) {
            GridRow {
                Text("Response").foregroundStyle(ClaimPalette.secondary)
                Text("Camera").bold()
                Text("LiDAR").bold()
            }
            comparisonRow("Books", key: "book_count")
            comparisonRow("Objects", key: "object_count")
            comparisonRow("Frames analyzed", key: "frames_processed")
            comparisonRow("Review entries", key: "review_count")
            GridRow {
                Text("Identified books")
                Text(coverage(model.comparison["camera"]["identity_coverage"]["fraction"]))
                Text(coverage(model.comparison["lidar"]["identity_coverage"]["fraction"]))
            }
            GridRow {
                Text("Measured books")
                Text(coverage(model.comparison["camera"]["measurement_coverage"]["fraction"]))
                Text(coverage(model.comparison["lidar"]["measurement_coverage"]["fraction"]))
            }
        }
        .font(.subheadline)
        .fixedSize(horizontal: true, vertical: false)
    }

    private var additionalMarketPanel: some View {
        DisclosureGroup {
            Text(model.secondLocale.pretty)
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(ClaimPalette.secondary)
                .padding(.top, 10)
                .textSelection(.enabled)
        } label: {
            Label("Additional pricing market", systemImage: "globe")
                .font(.subheadline.weight(.medium))
                .padding(.vertical, 5)
        }
        .claimCard()
    }

    private func comparisonRow(_ label: String, key: String) -> some View {
        GridRow {
            Text(label)
            Text(model.comparison["camera"][key].number.map { String(Int($0)) } ?? "Unknown")
            Text(model.comparison["lidar"][key].number.map { String(Int($0)) } ?? "Unknown")
        }
    }

    private func coverage(_ value: JSONValue) -> String {
        value.number.map { String(format: "%.0f%%", $0 * 100) } ?? "Unknown"
    }

    private func metric(_ name: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(name).font(.caption).foregroundStyle(ClaimPalette.secondary)
            Text(value)
                .font(.system(.title2, design: .serif).weight(.semibold))
                .foregroundStyle(ClaimPalette.forest)
                .fixedSize(horizontal: true, vertical: false)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

struct ObjectCameraPanel: View {
    @ObservedObject var overlays: ObjectOverlayController
    let isCapturing: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            ObjectCameraPreview(surface: overlays.surface)
                .frame(height: 420)
                .background(ClaimPalette.forest)
                .clipShape(RoundedRectangle(cornerRadius: 28))
                .overlay(alignment: .topLeading) { cameraBadge }
                .overlay {
                    RoundedRectangle(cornerRadius: 28)
                        .strokeBorder(.white.opacity(0.18), lineWidth: 1)
                        .allowsHitTesting(false)
                }
            Text(overlays.message)
                .font(.callout)
                .foregroundStyle(ClaimPalette.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 3)
            if !overlays.furniture.isEmpty { furniturePanel }
            if !overlays.detections.isEmpty { detectionPanel }
            DisclosureGroup {
                VStack(alignment: .leading, spacing: 8) {
                    Text(overlays.localStatus).fontWeight(.medium)
                    Text(overlays.serverStatus)
                    Text(overlays.depthStatus)
                }
                .font(.caption)
                .foregroundStyle(ClaimPalette.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, 10)
            } label: {
                Text("Camera & recognition details")
                    .font(.subheadline)
                    .padding(.vertical, 5)
            }
            .padding(.horizontal, 3)
        }
    }

    private var cameraBadge: some View {
        HStack(spacing: 7) {
            Image(systemName: isCapturing ? "record.circle" : "camera")
                .foregroundStyle(isCapturing ? ClaimPalette.gold : .white)
            Text(isCapturing ? "Live camera" : "Camera preview")
                .font(.caption.weight(.semibold))
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 13)
        .padding(.vertical, 9)
        .background(ClaimPalette.forest.opacity(0.92), in: Capsule())
        .padding(16)
        .accessibilityElement(children: .combine)
    }

    private var furniturePanel: some View {
        VStack(alignment: .leading, spacing: 12) {
            ClaimSectionHeading("Furniture in view", symbol: "chair.lounge")
            Text("LiDAR · live estimates")
                .font(.caption)
                .foregroundStyle(ClaimPalette.secondary)
            ForEach(overlays.furniture) { object in
                Text(object.category + " · " + object.dimensionsText)
                    .font(.subheadline)
                    .monospacedDigit()
            }
            Text("Move around furniture to refine its size. Glass, reflections and hidden edges can affect measurements.")
                .font(.caption)
                .foregroundStyle(ClaimPalette.secondary)
        }
        .claimCard()
    }

    private var detectionPanel: some View {
        VStack(alignment: .leading, spacing: 14) {
            ClaimSectionHeading("Objects in view", symbol: "viewfinder")
            Text("Dotted borders · mint: measured position · yellow: phone prediction or estimated position.")
                .font(.caption)
                .foregroundStyle(ClaimPalette.secondary)
            ScrollView(.horizontal) {
                HStack(alignment: .top, spacing: 10) {
                    ForEach(overlays.detections) { object in
                        VStack(alignment: .leading, spacing: 7) {
                            Text(object.label)
                                .font(.subheadline.weight(.semibold))
                                .fixedSize(horizontal: false, vertical: true)
                            if object.label != object.category {
                                Text(object.category).font(.caption)
                            }
                            Text(object.dimensionsText)
                                .font(.caption)
                                .foregroundStyle(ClaimPalette.secondary)
                            Text("Dotted outline")
                                .font(.caption2)
                                .foregroundStyle(ClaimPalette.secondary)
                        }
                        .frame(width: 170, alignment: .leading)
                        .padding(14)
                        .background(ClaimPalette.sage, in: RoundedRectangle(cornerRadius: 16))
                    }
                }
                .padding(.bottom, 4)
            }
            DisclosureGroup {
                VStack(alignment: .leading, spacing: 12) {
                    Text("This saved camera frame keeps every outline aligned, including objects that cannot yet be placed in the live view.")
                        .font(.caption)
                        .foregroundStyle(ClaimPalette.secondary)
                    AnalyzedFrameView(overlays: overlays)
                        .clipShape(RoundedRectangle(cornerRadius: 16))
                }
                .padding(.top, 10)
            } label: {
                Text("Last analyzed frame · \(overlays.detections.count) objects")
                    .font(.subheadline)
                    .padding(.vertical, 5)
            }
        }
        .claimCard()
    }
}

struct RoomMeasurementsPanel: View {
    let room: JSONValue
    let mode: CaptureMode
    let isCapturing: Bool
    let detectedFloorCount: Int
    let detectedWallCount: Int
    let scanStatus: String
    let coaching: String

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            ClaimSectionHeading("Room measurements", symbol: "ruler")
            if isCapturing {
                Text("Room scan in progress")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(ClaimPalette.secondary)
            }
            if mode == .camera {
                Text("LiDAR is unavailable on this device. Camera fallback records visible contents; physical room dimensions remain unknown.")
                    .font(.callout)
                    .foregroundStyle(ClaimPalette.secondary)
            } else {
                Text("\(detectedFloorCount) floor surfaces · \(detectedWallCount) walls detected")
                    .font(.subheadline.weight(.medium))
                    .monospacedDigit()
                if !scanStatus.isEmpty {
                    Text(scanStatus)
                        .font(.callout)
                        .foregroundStyle(ClaimPalette.secondary)
                }
                if !coaching.isEmpty {
                    Label(coaching,systemImage:"viewfinder")
                        .font(.callout)
                }
                let summary = RoomMeasurementSummary(value: room)
                Text(summary.size)
                    .font(.system(.title3, design: .rounded).weight(.medium))
                    .monospacedDigit()
                Text(summary.area)
                    .font(.callout)
                    .monospacedDigit()
                    .foregroundStyle(ClaimPalette.secondary)
                ForEach(Array(room["furniture"].array.enumerated()), id: \.offset) { _, object in
                    Text((object["category"].string.map(displayCategory) ?? "Furniture") + " · " + InventoryRow(value: object).dimensionsText)
                        .font(.subheadline)
                        .monospacedDigit()
                }
                if !isCapturing {
                    ForEach(Array(room["review_reasons"].array.enumerated()), id: \.offset) { _, reason in
                        if let text = reason.string {
                            Label(text, systemImage: "info.circle")
                                .font(.caption)
                                .foregroundStyle(ClaimPalette.secondary)
                        }
                    }
                }
                DisclosureGroup {
                    Text("Floor area is calculated from the measured outline after the sweep ends. Missing outlines remain unknown. Length and width are the outline’s overall extents; height comes from walls.")
                        .font(.caption)
                        .foregroundStyle(ClaimPalette.secondary)
                        .padding(.top, 10)
                } label: {
                    Text("How measurements are calculated")
                        .font(.subheadline)
                        .padding(.vertical, 5)
                }
            }
        }
        .claimCard()
    }
}

private struct InventoryListRow: View {
    let row: InventoryRow
    let open: () -> Void

    var body: some View {
        VStack(alignment:.leading,spacing:12) {
            Button(action:open) {
                HStack(alignment: .center, spacing: 12) {
                    Image(systemName: "doc.text")
                        .font(.title3)
                        .foregroundStyle(ClaimPalette.forest)
                        .frame(width: 42, height: 46)
                        .background(ClaimPalette.sage, in: RoundedRectangle(cornerRadius: 12))
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 5) {
                        Text(row.title).font(.subheadline.weight(.semibold))
                        Text(row.subtitle).font(.caption).foregroundStyle(ClaimPalette.secondary)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(ClaimPalette.secondary)
                        .accessibilityHidden(true)
                }
                .frame(minHeight:60)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityHint("Opens item details")
            InventoryValuationView(row:row,compact:true)
        }
        .padding(.vertical, 14)
    }
}

private struct InventoryDetailSheet: View {
    @ObservedObject var model: CaptureController
    let selection: InventorySelection
    let dismiss: () -> Void

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if let row = selection.resolve(in:model.rows) {
                        InventoryDetailsContent(row:row)
                    } else {
                        Text("This item is no longer in the current inventory.")
                            .font(.callout)
                    }
                }
                .padding(20)
            }
            .background(ClaimPalette.ivory)
            .navigationTitle("Item details")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { Button("Done", action: dismiss) }
        }
        .tint(ClaimPalette.forest)
        .foregroundStyle(ClaimPalette.ink)
        .presentationDragIndicator(.visible)
        .preferredColorScheme(.light)
    }
}

private struct InventoryDetailsContent: View {
    let row: InventoryRow

    var body: some View {
        Text(row.title)
            .font(.system(.title,design:.serif).weight(.semibold))
            .foregroundStyle(ClaimPalette.forest)
        Text(row.subtitle).font(.callout).foregroundStyle(ClaimPalette.secondary)
        InventoryValuationView(row:row).claimCard()
        DisclosureGroup {
            Text(row.value.pretty)
                .font(.system(.caption,design:.monospaced))
                .frame(maxWidth:.infinity,alignment:.leading)
                .padding(.top,12)
                .textSelection(.enabled)
        } label: {
            Label("Evidence details",systemImage:"doc.text.magnifyingglass")
                .font(.subheadline.weight(.medium)).padding(.vertical,5)
        }
        .claimCard()
    }
}

@MainActor private final class SavedInventoryBrowser: ObservableObject {
    @Published var summaries: [SavedInventorySummary] = []
    @Published private(set) var selection: SavedInventorySummary?
    @Published private(set) var packet: JSONValue = .null
    @Published private(set) var error = ""
    @Published private(set) var loading = false
    @Published private(set) var loadedList = false
    @Published private(set) var updatedAt: Date?
    @Published private var refreshGeneration = UUID()
    private var client: BackendClient?

    var rows: [InventoryRow] { (packet["books"].array + packet["items"].array).map { InventoryRow(value:$0) } }
    var refreshKey: String { (selection?.id ?? "list") + refreshGeneration.uuidString }
    var completionText: String {
        packet == .null ? (selection?.completion ?? "Saved inventory")
            : SavedInventorySummary.completionText(packet["completion"].string)
    }

    func select(_ summary: SavedInventorySummary?) {
        selection = summary; packet = .null; updatedAt = nil; error = ""
        refreshGeneration = UUID()
    }
    func refresh() { error = ""; refreshGeneration = UUID() }

    func load(using capture: CaptureController) async {
        let key = refreshKey, selectedID = selection?.id
        loading = true
        defer { if key == refreshKey { loading = false } }
        do {
            if client == nil { client = try capture.savedInventoryClient() }
            guard let client else { throw BackendError.disconnected }
            if let selectedID {
                while !Task.isCancelled {
                    let value = try await client.call("api/sweeps/" + selectedID,timeout:15)
                    try Task.checkCancellation()
                    guard key == refreshKey else { return }
                    guard value["sweep"]["id"].string == selectedID,
                          value["sweep"]["synthetic"] != .bool(true) else {
                        packet = .null; updatedAt = nil
                        throw BackendError.response(422,"This saved inventory is unavailable for review.")
                    }
                    packet = value; updatedAt = Date(); error = ""; loading = false
                    try await Task.sleep(for:.seconds(4))
                }
            } else {
                let value = try await client.call("api/sweeps",timeout:15)
                try Task.checkCancellation()
                guard key == refreshKey else { return }
                guard case .array = value else { throw BackendError.response(422,"Saved inventories could not be read.") }
                summaries = value.array.prefix(20).compactMap(SavedInventorySummary.init)
                loadedList = true; error = ""
            }
        } catch {
            guard !Task.isCancelled, key == refreshKey else { return }
            self.error = error.localizedDescription // Retain the last supported prices; pause automatic retries.
        }
    }
}

private struct SavedInventoriesSheet: View {
    @ObservedObject var capture: CaptureController
    @StateObject private var browser = SavedInventoryBrowser()
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    @State private var selectedItem: InventorySelection?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment:.leading,spacing:18) {
                    if let summary = browser.selection {
                        Text(summary.dateText).font(.title3.weight(.semibold))
                        Text(summary.country + " · " + browser.completionText)
                            .font(.caption).foregroundStyle(ClaimPalette.secondary)
                        if browser.packet != .null {
                            Text("\(browser.packet["books"].array.count) books · \(browser.packet["items"].array.count) objects")
                                .font(.headline)
                            if browser.rows.isEmpty { Text("No items were saved in this inventory.").font(.callout) }
                            ForEach(browser.rows) { row in
                                InventoryListRow(row:row) { selectedItem = InventorySelection(id:row.id) }
                                Divider().overlay(ClaimPalette.line)
                            }
                        }
                        if let date = browser.updatedAt {
                            Text("Updated " + date.formatted(date:.omitted,time:.standard))
                                .font(.caption2).foregroundStyle(ClaimPalette.secondary)
                        }
                    } else {
                        Text("Review saved items and their latest sourced estimates.")
                            .font(.callout).foregroundStyle(ClaimPalette.secondary)
                        ForEach(browser.summaries) { summary in
                            Button { browser.select(summary) } label: {
                                HStack(spacing:12) {
                                    Image(systemName:"books.vertical")
                                    VStack(alignment:.leading,spacing:6) {
                                        Text(summary.dateText).font(.headline)
                                        Text(summary.country + " · " + summary.completion).font(.caption)
                                    }
                                    Spacer()
                                    Image(systemName:"chevron.right").font(.caption)
                                }
                                .frame(maxWidth:.infinity,minHeight:60,alignment:.leading)
                            }
                            .buttonStyle(.plain)
                            Divider().overlay(ClaimPalette.line)
                        }
                        if browser.loadedList, browser.summaries.isEmpty {
                            Text("No saved room inventories are available yet.").font(.callout)
                        }
                    }
                    if browser.loading { ProgressView("Loading saved inventory…").tint(ClaimPalette.forest) }
                    if !browser.error.isEmpty {
                        Text(browser.error).font(.callout).foregroundStyle(ClaimPalette.secondary)
                        Button("Retry") { browser.refresh() }.buttonStyle(ClaimSecondaryButtonStyle())
                    }
                }
                .padding(20)
            }
            .background(ClaimPalette.ivory)
            .navigationTitle(browser.selection == nil ? "Saved inventories" : "Saved inventory")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement:.topBarLeading) {
                    if browser.selection != nil { Button("All inventories") { browser.select(nil) } }
                }
                ToolbarItemGroup(placement:.topBarTrailing) {
                    Button { browser.refresh() } label: { Image(systemName:"arrow.clockwise") }
                        .accessibilityLabel("Refresh saved inventory")
                    Button("Done") { dismiss() }
                }
            }
            .task(id:scenePhase == .active ? browser.refreshKey : "paused") {
                guard scenePhase == .active else { return }
                await browser.load(using:capture)
            }
            .sheet(item:$selectedItem) { selection in
                SavedInventoryItemSheet(browser:browser,selection:selection)
            }
        }
        .tint(ClaimPalette.forest).foregroundStyle(ClaimPalette.ink)
        .presentationDragIndicator(.visible).preferredColorScheme(.light)
    }
}

private struct SavedInventoryItemSheet: View {
    @ObservedObject var browser: SavedInventoryBrowser
    let selection: InventorySelection
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment:.leading,spacing:18) {
                    if let row = selection.resolve(in:browser.rows) { InventoryDetailsContent(row:row) }
                    else { Text("This item is no longer in the saved inventory.") }
                    if !browser.error.isEmpty { Text(browser.error).font(.caption) }
                }
                .padding(20)
            }
            .background(ClaimPalette.ivory)
            .navigationTitle("Item details").navigationBarTitleDisplayMode(.inline)
            .toolbar { Button("Done") { dismiss() } }
        }
        .tint(ClaimPalette.forest).foregroundStyle(ClaimPalette.ink)
        .presentationDragIndicator(.visible).preferredColorScheme(.light)
    }
}

private struct InventoryValuationView: View {
    let row: InventoryRow
    var compact = false

    var body: some View {
        VStack(alignment:.leading,spacing:12) {
            if let estimate = row.replacementEstimate {
                InventoryEstimateView(title:"Estimated replacement",estimate:estimate,compact:compact)
            } else if !compact {
                unavailable("Estimated replacement",value:row.value["replacement_cost"])
            }
            if let estimate = row.resaleEstimate {
                InventoryEstimateView(title:"Estimated resale",estimate:estimate,compact:compact)
            } else if !compact, row.hasResaleField {
                unavailable("Estimated resale",value:row.value["used_value"])
            }
            HStack(alignment:.center,spacing:8) {
                if !row.isExcluded, !row.needsAppraisal, [.queued,.searching].contains(row.pricingStatus) {
                    ProgressView().controlSize(.mini).tint(ClaimPalette.forest)
                }
                Text(row.pricingStatusText)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(ClaimPalette.secondary)
            }
            if !row.pricingReason.isEmpty {
                Text(row.pricingReason)
                    .font(.caption)
                    .foregroundStyle(ClaimPalette.secondary)
                    .lineLimit(compact ? 2 : nil)
            }
        }
        .frame(maxWidth:.infinity,alignment:.leading)
    }

    private func unavailable(_ title: String, value: JSONValue) -> some View {
        VStack(alignment:.leading,spacing:5) {
            Text(title + " · unknown")
                .font(.subheadline.weight(.medium))
            if !row.isExcluded, !row.needsAppraisal,
               let reason = value["exclusion_reason"].string, !reason.isEmpty {
                Text(reason)
                    .font(.caption)
                    .foregroundStyle(ClaimPalette.secondary)
            }
        }
    }
}

private struct InventoryEstimateView: View {
    let title: String
    let estimate: InventoryPriceEstimate
    let compact: Bool

    var body: some View {
        VStack(alignment:.leading,spacing:5) {
            Text(title)
                .font(.caption)
                .foregroundStyle(ClaimPalette.secondary)
            Text(estimate.moneyText)
                .font(compact ? .subheadline.weight(.semibold) : .title3.weight(.semibold))
                .monospacedDigit()
            Text(estimate.matchLabel)
                .font(.caption)
                .foregroundStyle(ClaimPalette.secondary)
            if !compact {
                if !estimate.productName.isEmpty { Text(estimate.productName).font(.subheadline) }
                if !estimate.matchBasis.isEmpty { Text(estimate.matchBasis).font(.caption) }
                if !estimate.condition.isEmpty { Text("Offer condition: " + estimate.condition).font(.caption) }
                if estimate.converted { Text("Converted using the saved exchange-rate evidence.").font(.caption) }
            }
            Link(destination:estimate.sourceURL) {
                Label(estimate.sourceTitle,systemImage:"arrow.up.right")
                    .font(.caption.weight(.medium))
            }
            Text("Retrieved " + estimate.retrievedDateText)
                .font(.caption2)
                .foregroundStyle(ClaimPalette.secondary)
        }
        .frame(maxWidth:.infinity,alignment:.leading)
    }
}

private struct ClaimSectionHeading: View {
    let title: String
    let symbol: String

    init(_ title: String, symbol: String) {
        self.title = title
        self.symbol = symbol
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 9) {
            Image(systemName: symbol)
                .font(.subheadline)
                .accessibilityHidden(true)
            Text(title)
                .font(.system(.title3, design: .serif).weight(.semibold))
        }
        .foregroundStyle(ClaimPalette.forest)
        .accessibilityAddTraits(.isHeader)
    }
}

private struct ClaimActionLabel: View {
    let title: String
    let symbol: String
    var isLoading = false

    var body: some View {
        HStack(spacing: 10) {
            if isLoading {
                ProgressView().tint(.white)
            } else {
                Image(systemName: symbol).accessibilityHidden(true)
            }
            Text(title).multilineTextAlignment(.center)
        }
        .font(.headline)
        .frame(maxWidth: .infinity, minHeight: 30)
        .padding(.vertical, 12)
        .padding(.horizontal, 16)
        .contentShape(Rectangle())
    }
}

private struct ClaimPrimaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(.white)
            .background(ClaimPalette.forest.opacity(isEnabled ? 1 : 0.55), in: RoundedRectangle(cornerRadius: 18))
            .opacity(configuration.isPressed ? 0.82 : 1)
            .scaleEffect(configuration.isPressed ? 0.985 : 1)
            .animation(.easeOut(duration: 0.15), value: configuration.isPressed)
    }
}

private struct ClaimSecondaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.subheadline.weight(.medium))
            .foregroundStyle(ClaimPalette.forest)
            .padding(.horizontal, 16)
            .padding(.vertical, 6)
            .background(ClaimPalette.sage, in: RoundedRectangle(cornerRadius: 16))
            .opacity(configuration.isPressed ? 0.65 : 1)
    }
}

private struct ClaimCardModifier: ViewModifier {
    func body(content: Content) -> some View {
        content
            .padding(18)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.white.opacity(0.86), in: RoundedRectangle(cornerRadius: 24))
            .overlay {
                RoundedRectangle(cornerRadius: 24)
                    .strokeBorder(ClaimPalette.line.opacity(0.8), lineWidth: 1)
                    .allowsHitTesting(false)
            }
    }
}

private extension View {
    func claimCard() -> some View { modifier(ClaimCardModifier()) }
}

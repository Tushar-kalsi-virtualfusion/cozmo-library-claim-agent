"""Reproduce the dependency-free iOS Xcode project without XcodeGen/CocoaPods."""
import hashlib
import re
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "ios"
project = root / "LibraryClaim.xcodeproj"
project.mkdir(exist_ok=True)
existing_project = project / "project.pbxproj"
existing_text = existing_project.read_text() if existing_project.exists() else ""
team_match = re.search(r'DEVELOPMENT_TEAM = ([A-Z0-9]+);', existing_text)
team = team_match[1] if team_match else '""'
uid = lambda name: hashlib.sha256(name.encode()).hexdigest()[:24].upper()
objects = []


def obj(name, body):
    objects.append(f"{uid(name)} = {{ {body} }};")
    return uid(name)


sources = []
resources = []
files = []
for file in sorted((root / "LibraryClaim").glob("*.swift")):
    ref = obj("ref:" + file.name, f'isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = "{file.name}"; sourceTree = "<group>";')
    files.append(ref)
    sources.append(obj("build:" + file.name, f"isa = PBXBuildFile; fileRef = {ref};"))
model = obj("ref:household", 'isa = PBXFileReference; lastKnownFileType = folder.mlpackage; path = Models/household.mlpackage; sourceTree = "<group>";')
files.append(model)
sources.append(obj("build:household", f"isa = PBXBuildFile; fileRef = {model};"))
assets = obj("ref:assets", 'isa = PBXFileReference; lastKnownFileType = folder.assetcatalog; path = Assets.xcassets; sourceTree = "<group>";')
files.append(assets)
resources.append(obj("build:assets", f"isa = PBXBuildFile; fileRef = {assets};"))
info = obj("info", 'isa = PBXFileReference; lastKnownFileType = text.plist.xml; path = Info.plist; sourceTree = "<group>";')
connection = obj("connection", 'isa = PBXFileReference; lastKnownFileType = text.xcconfig; path = Connection.xcconfig; sourceTree = "<group>";')
app = obj("app", 'isa = PBXFileReference; explicitFileType = wrapper.application; includeInIndex = 0; path = LibraryClaim.app; sourceTree = BUILT_PRODUCTS_DIR;')
obj("source-group", f'isa = PBXGroup; children = ({",".join(files+[info])},); path = LibraryClaim; sourceTree = "<group>";')
obj("products", f'isa = PBXGroup; children = ({app},); name = Products; sourceTree = "<group>";')
obj("main-group", f'isa = PBXGroup; children = ({uid("source-group")},{connection},{uid("products")},); sourceTree = "<group>";')
obj("sources", f'isa = PBXSourcesBuildPhase; buildActionMask = 2147483647; files = ({",".join(sources)},); runOnlyForDeploymentPostprocessing = 0;')
obj("frameworks", 'isa = PBXFrameworksBuildPhase; buildActionMask = 2147483647; files = (); runOnlyForDeploymentPostprocessing = 0;')
obj("resources", f'isa = PBXResourcesBuildPhase; buildActionMask = 2147483647; files = ({",".join(resources)},); runOnlyForDeploymentPostprocessing = 0;')
for name in ["Debug", "Release"]:
    obj("project-"+name, f'isa = XCBuildConfiguration; name = {name}; buildSettings = {{ SDKROOT = iphoneos; IPHONEOS_DEPLOYMENT_TARGET = 17.0; CLANG_ENABLE_MODULES = YES; SWIFT_VERSION = 5.0; }};')
    optimization = "-Onone" if name == "Debug" else "-O"
    conditions = "DEBUG" if name == "Debug" else '""'
    obj("target-"+name, f'''isa = XCBuildConfiguration; name = {name}; baseConfigurationReference = {connection}; buildSettings = {{
        PRODUCT_NAME = "$(TARGET_NAME)"; PRODUCT_BUNDLE_IDENTIFIER = com.tusharkalsi.libraryclaim;
        INFOPLIST_FILE = LibraryClaim/Info.plist; GENERATE_INFOPLIST_FILE = NO;
        CODE_SIGN_STYLE = Automatic; DEVELOPMENT_TEAM = {team}; TARGETED_DEVICE_FAMILY = 1;
        SUPPORTED_PLATFORMS = "iphoneos iphonesimulator"; SUPPORTS_MACCATALYST = NO;
        CURRENT_PROJECT_VERSION = 1; MARKETING_VERSION = 0.1; SWIFT_VERSION = 5.0;
        SWIFT_OPTIMIZATION_LEVEL = "{optimization}"; ENABLE_USER_SCRIPT_SANDBOXING = YES;
        SWIFT_ACTIVE_COMPILATION_CONDITIONS = {conditions};
        LD_RUNPATH_SEARCH_PATHS = ("$(inherited)","@executable_path/Frameworks");
        SWIFT_EMIT_LOC_STRINGS = YES; ASSETCATALOG_COMPILER_APPICON_NAME = AppIcon;
    }};''')
for owner in ["project", "target"]:
    obj(owner+"-config", f'isa = XCConfigurationList; buildConfigurations = ({uid(owner+"-Debug")},{uid(owner+"-Release")},); defaultConfigurationIsVisible = 0; defaultConfigurationName = Release;')
obj("target", f'''isa = PBXNativeTarget; buildConfigurationList = {uid("target-config")};
buildPhases = ({uid("sources")},{uid("frameworks")},{uid("resources")},); buildRules = (); dependencies = ();
name = LibraryClaim; productName = LibraryClaim; productReference = {app}; productType = "com.apple.product-type.application";''')
obj("project", f'''isa = PBXProject; attributes = {{ LastUpgradeCheck = 1600; TargetAttributes = {{ {uid("target")} = {{ CreatedOnToolsVersion = 16.0; }}; }}; }};
buildConfigurationList = {uid("project-config")}; compatibilityVersion = "Xcode 14.0";
developmentRegion = en; hasScannedForEncodings = 0; knownRegions = (en,Base,);
mainGroup = {uid("main-group")}; productRefGroup = {uid("products")}; projectDirPath = ""; projectRoot = ""; targets = ({uid("target")},);''')
(project / "project.pbxproj").write_text("// !$*UTF8*$!\n{ archiveVersion = 1; classes = {}; objectVersion = 56; objects = {\n"+"\n".join(objects)+f"\n}}; rootObject = {uid('project')}; }}\n")
schemes = project / "xcshareddata/xcschemes"
schemes.mkdir(parents=True,exist_ok=True)
reference = f'<BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="{uid("target")}" BuildableName="LibraryClaim.app" BlueprintName="LibraryClaim" ReferencedContainer="container:LibraryClaim.xcodeproj"/>'
scheme = schemes / "LibraryClaim.xcscheme"
# Preserve Xcode edits to an existing shared launch scheme.
if not scheme.exists():
    scheme.write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<Scheme LastUpgradeVersion="1600" version="1.3">
<BuildAction parallelizeBuildables="YES" buildImplicitDependencies="YES"><BuildActionEntries><BuildActionEntry buildForTesting="YES" buildForRunning="YES" buildForProfiling="YES" buildForArchiving="YES" buildForAnalyzing="YES">{reference}</BuildActionEntry></BuildActionEntries></BuildAction>
<LaunchAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB" launchStyle="0" useCustomWorkingDirectory="NO" ignoresPersistentStateOnLaunch="NO" debugDocumentVersioning="YES" debugServiceExtension="internal" allowLocationSimulation="NO"><BuildableProductRunnable runnableDebuggingMode="0">{reference}</BuildableProductRunnable></LaunchAction>
<ProfileAction buildConfiguration="Release" shouldUseLaunchSchemeArgsEnv="YES" savedToolIdentifier="" useCustomWorkingDirectory="NO" debugDocumentVersioning="YES"><BuildableProductRunnable runnableDebuggingMode="0">{reference}</BuildableProductRunnable></ProfileAction>
<AnalyzeAction buildConfiguration="Debug"/><ArchiveAction buildConfiguration="Release" revealArchiveInOrganizer="YES"/>
</Scheme>''')
print(project)

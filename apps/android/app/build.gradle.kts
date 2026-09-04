import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// Built by `npm run build` in services/memory-gateway/ui; copied into assets/ui.
val consoleUiDist = rootProject.file("../../services/memory-gateway/ui/dist")
val consoleUiAssets = layout.buildDirectory.dir("generated/consoleUi")

android {
    namespace = "app.memoryplatform.android"
    compileSdk = 35

    defaultConfig {
        applicationId = "app.memoryplatform.android"
        minSdk = 26
        targetSdk = 35
        versionCode = 3
        versionName = "0.5.2"
        // Chaquopy needs an explicit ABI list. arm64 covers every phone that
        // matters; add "x86_64" only for emulator builds (needs x86_64 Rust wheels too).
        ndk { abiFilters += listOf("arm64-v8a") }
    }

    // Release signing: apps/android/keystore.properties (gitignored) points at a
    // keystore kept outside the repo. Without it, release builds are unsigned.
    val keystoreProps = rootProject.file("keystore.properties")
    if (keystoreProps.isFile) {
        val props = Properties().apply { keystoreProps.inputStream().use { stream -> load(stream) } }
        signingConfigs {
            create("release") {
                storeFile = file(props.getProperty("storeFile"))
                storePassword = props.getProperty("storePassword")
                keyAlias = props.getProperty("keyAlias")
                keyPassword = props.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (keystoreProps.isFile) signingConfig = signingConfigs.getByName("release")
        }
    }

    applicationVariants.all {
        outputs.all {
            (this as? com.android.build.gradle.internal.api.BaseVariantOutputImpl)?.outputFileName =
                "memory-platform-android-${versionName}-${name}.apk"
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }

    sourceSets["main"].assets.srcDir(consoleUiAssets)

}

chaquopy {
    defaultConfig {
        version = "3.14"  // must match the Python that built the Rust wheels (Termux: 3.14)
        pip {
            // Local wheels: the three first-party packages (pure Python, built by
            // scripts/android/build-wheels.sh) and the two cross-compiled Rust
            // wheels (scripts/android/build-rust-wheels.sh).
            // --no-deps: the first-party wheels declare uvicorn[standard], whose
            // native extras cannot be built for Android. requirements-embedded.txt
            // is the complete, pinned closure instead (verified with `pip check`).
            options("--no-deps", "--find-links", rootProject.file("wheels").absolutePath)
            install("-r", rootProject.file("requirements-embedded.txt").absolutePath)
            install("model_gateway_contracts==0.5.1")
            install("local_model_gateway==0.5.1")
            install("memory_gateway==0.5.1")
        }
    }
}

// The first-party wheels keep the same version number between builds, so make
// the pip-install task watch the wheel files themselves; otherwise Gradle
// considers it up to date and the APK silently ships the previous code.
tasks.matching { it.name.matches(Regex("install\\w+PythonRequirements")) }.configureEach {
    inputs.dir(rootProject.file("wheels"))
    inputs.file(rootProject.file("requirements-embedded.txt"))
}

// Chaquopy ships libsqlite3_python.so without FTS5, which disables the knowledge
// base and the memory keyword index. Replace it in Chaquopy's generated jniLibs
// with our build (scripts/android/build-sqlite-fts5.sh): same SQLite version,
// superset of exported symbols. Chaquopy appends its copy action late, so this
// runs as its own task between generate*PythonJniLibs and AGP's jniLibs merge.
val fts5Sqlite = rootProject.file("native/arm64-v8a/libsqlite3_python.so")
afterEvaluate {
    listOf("Debug", "Release").forEach { variant ->
        val patch = tasks.register("patch${variant}SqliteFts5") {
            dependsOn("generate${variant}PythonJniLibs")
            inputs.file(fts5Sqlite)
            doLast {
                check(fts5Sqlite.isFile) { "missing $fts5Sqlite: run scripts/android/build-sqlite-fts5.sh" }
                val dir = layout.buildDirectory.dir("python/jniLibs/${variant.lowercase()}").get().asFile
                val targets = dir.walkTopDown().filter { it.isFile && it.name == "libsqlite3_python.so" }.toList()
                check(targets.isNotEmpty()) { "Chaquopy jniLibs output not found under $dir" }
                targets.forEach { target -> fts5Sqlite.copyTo(target, overwrite = true) }
                println("patched ${targets.size} libsqlite3_python.so with FTS5 build")
            }
        }
        tasks.named("merge${variant}JniLibFolders") { dependsOn(patch) }
    }
}

// Sync, not Copy: a Copy task leaves the previous build's hashed bundles in the
// generated assets dir, and they end up in the APK next to the new ones.
val copyConsoleUi by tasks.registering(Sync::class) {
    doFirst {
        check(consoleUiDist.resolve("index.html").isFile) {
            "Web console build missing: run `npm run build` in services/memory-gateway/ui first"
        }
    }
    from(consoleUiDist)
    into(consoleUiAssets.map { it.dir("ui") })
}
tasks.named("preBuild") { dependsOn(copyConsoleUi) }

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.lifecycle:lifecycle-livedata-ktx:2.8.7")
    implementation("com.google.android.material:material:1.12.0")
}

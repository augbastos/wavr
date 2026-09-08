package dev.wavr

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File
import java.io.FileInputStream
import javax.xml.parsers.DocumentBuilderFactory

/**
 * The Core launcher's string catalogue, checked against its Brazilian
 * Portuguese translation.
 *
 * WHY THIS IS A TEST AND NOT A CONVENTION. The Core launcher is the Wavr Core
 * as a household actually meets it: its foreground-service notification is one
 * of the very few places a person sees what the Core is doing without opening
 * the dashboard. A string that exists only in English there is not a cosmetic
 * gap — it is that surface going quiet for the reader. The failure mode is also
 * invisible: Android resolves a missing translation by silently serving the
 * English string, so nothing errors and nobody notices until a Brazilian
 * household reads its own phone.
 *
 * This runs on the JVM and needs no device: it parses the two `strings.xml`
 * files off disk. That is deliberate — anything needing an emulator would not
 * run in the loop where translations are actually added.
 *
 * It guards four things:
 *   1. Every `<string>` in `values/` has an entry in `values-pt-rBR/`.
 *   2. `values-pt-rBR/` declares no string `values/` does not (such an entry is
 *      unreferenced and dead in every other locale).
 *   3. Format placeholders agree per string, in index AND conversion. This is
 *      the one that crashes rather than merely reads badly: a translation whose
 *      `%1$s` became `%1$d` throws IllegalFormatConversionException inside
 *      CoreService.buildNotification(), i.e. inside the foreground service.
 *   4. The translation is Brazilian, and is actually a translation — no English
 *      pasted through, no European Portuguese lexicon.
 */
class StringResourceLocalizationTest {

    // -- Where the resources live --------------------------------------------

    /**
     * Gradle runs unit tests with the module directory as the working
     * directory, but that is a default rather than a promise, so walk up
     * looking for the res tree instead of trusting one relative path. A test
     * that passes because it found no files at all is worse than no test, so
     * not finding them is itself a failure.
     */
    private fun moduleResDir(): File {
        var dir: File? = File(System.getProperty("user.dir") ?: ".").absoluteFile
        val tried = mutableListOf<String>()
        repeat(8) {
            val here = dir ?: return@repeat
            for (candidate in listOf(
                File(here, "src/main/res"),
                File(here, "app/src/main/res"),
            )) {
                tried.add(candidate.path)
                if (File(candidate, "values/strings.xml").isFile) return candidate
            }
            dir = here.parentFile
        }
        fail("Could not find the launcher's res/ directory. Looked in:\n  " + tried.joinToString("\n  "))
        error("unreachable")
    }

    private fun defaultFile() = File(moduleResDir(), "values/strings.xml")

    private fun ptBrFile() = File(moduleResDir(), "values-pt-rBR/strings.xml")

    // -- Parsing --------------------------------------------------------------

    /** name -> literal value, in document order. A duplicate name is a failure, not a merge. */
    private fun readStrings(file: File): LinkedHashMap<String, String> {
        assertTrue("Missing string resource file: ${file.path}", file.isFile)
        val factory = DocumentBuilderFactory.newInstance().apply {
            isNamespaceAware = false
            isValidating = false
            // This parses a file from the repo and should not be able to reach
            // anywhere else. Not every parser knows the feature; not knowing it
            // is fine, silently enabling it would not be.
            runCatching {
                setFeature("http://apache.org/xml/features/nonvalidating/load-external-dtd", false)
            }
        }
        val doc = FileInputStream(file).use { factory.newDocumentBuilder().parse(it) }
        val nodes = doc.getElementsByTagName("string")
        val out = LinkedHashMap<String, String>()
        for (i in 0 until nodes.length) {
            val el = nodes.item(i)
            val name = el.attributes?.getNamedItem("name")?.nodeValue
            if (name == null) {
                fail("A <string> element in ${file.path} has no name attribute.")
                continue
            }
            val previous = out.put(name, el.textContent ?: "")
            if (previous != null) {
                fail(
                    "Duplicate <string name=\"$name\"> in ${file.path}. aapt keeps the " +
                        "last one, so the first is dead text that still reads as shipped."
                )
            }
        }
        assertTrue("No <string> entries parsed out of ${file.path}", out.isNotEmpty())
        return out
    }

    /**
     * Every `<plurals>` arm, keyed "name/quantity".
     *
     * The checks above read `<string>` and nothing else, so the day a count
     * became a plural — which is the day "Watching 1 rooms." was fixed — the
     * whole resource left this file's sight. A plural with no Portuguese, or
     * with an arm missing, would have read as complete.
     */
    private fun readPlurals(file: File): LinkedHashMap<String, String> {
        val factory = DocumentBuilderFactory.newInstance().apply {
            isNamespaceAware = false
            isValidating = false
            runCatching {
                setFeature("http://apache.org/xml/features/nonvalidating/load-external-dtd", false)
            }
        }
        val doc = FileInputStream(file).use { factory.newDocumentBuilder().parse(it) }
        val groups = doc.getElementsByTagName("plurals")
        val out = LinkedHashMap<String, String>()
        for (i in 0 until groups.length) {
            val group = groups.item(i)
            val name = group.attributes?.getNamedItem("name")?.nodeValue ?: continue
            val items = group.childNodes
            for (j in 0 until items.length) {
                val item = items.item(j)
                if (item.nodeName != "item") continue
                val qty = item.attributes?.getNamedItem("quantity")?.nodeValue ?: continue
                out["$name/$qty"] = item.textContent ?: ""
            }
        }
        return out
    }

    /**
     * Every format specifier, normalised to "index:conversion".
     *
     * Positional (`%1$s`) and bare (`%s`) both occur in Android catalogues; bare
     * ones are numbered by order of appearance so the two forms compare.
     */
    private fun placeholders(value: String): List<String> {
        var implicit = 0
        return SPEC.findAll(value).map { m ->
            val index = m.groupValues[1].ifEmpty { (++implicit).toString() }
            "$index:${m.groupValues[2]}"
        }.sorted().toList()
    }

    // -- The tests ------------------------------------------------------------

    @Test
    fun everyEnglishStringHasABrazilianPortugueseTranslation() {
        val en = readStrings(defaultFile())
        val pt = readStrings(ptBrFile())

        val missing = en.keys.filter { it !in pt.keys }
        if (missing.isNotEmpty()) {
            fail(
                "${missing.size} string(s) in values/strings.xml have no entry in " +
                    "values-pt-rBR/strings.xml, so a Brazilian household reads them in " +
                    "English and nothing anywhere reports it:\n  " +
                    missing.joinToString("\n  ") { "$it = \"${en[it]}\"" }
            )
        }
    }

    @Test
    fun theTranslationDeclaresNoStringTheSourceDoesNotHave() {
        val en = readStrings(defaultFile())
        val pt = readStrings(ptBrFile())

        val extra = pt.keys.filter { it !in en.keys }
        assertEquals(
            "values-pt-rBR/strings.xml declares string(s) that values/strings.xml does not. " +
                "Nothing can reference them and every other locale has nothing to fall back " +
                "to: $extra",
            emptyList<String>(),
            extra,
        )
    }

    @Test
    fun formatPlaceholdersMatchBetweenTheTwoLocales() {
        val en = readStrings(defaultFile())
        val pt = readStrings(ptBrFile())

        val broken = mutableListOf<String>()
        for ((name, english) in en) {
            val translated = pt[name] ?: continue // reported by the missing-translation test
            val a = placeholders(english)
            val b = placeholders(translated)
            if (a != b) broken.add("$name: values=$a  pt-rBR=$b")
        }
        assertEquals(
            "Placeholder mismatch. A dropped argument silently blanks part of the " +
                "notification; a changed conversion (%1\$s -> %1\$d) throws inside the " +
                "foreground service:\n  " + broken.joinToString("\n  "),
            emptyList<String>(),
            broken,
        )
    }

    @Test
    fun everyPluralIsTranslatedArmForArm() {
        val en = readPlurals(defaultFile())
        val pt = readPlurals(ptBrFile())

        val missing = en.keys - pt.keys
        assertEquals(
            "These plural arms have no pt-BR. An arm the translation omits falls back " +
                "to English for that count only, which is the hardest kind of gap to " +
                "notice — the notification reads Portuguese until somebody has exactly " +
                "one room: $missing",
            emptySet<String>(),
            missing,
        )

        // Both catalogues must at least offer `one` and `other`, which is what
        // Android needs to pick an arm at all.
        val names = en.keys.map { it.substringBefore('/') }.toSet()
        val incomplete = names.filter { n ->
            listOf("one", "other").any { q -> "$n/$q" !in en || "$n/$q" !in pt }
        }
        assertEquals(
            "A plural missing its `one` or `other` arm: Android falls back to the raw " +
                "resource name for the count it cannot resolve: $incomplete",
            emptyList<String>(),
            incomplete,
        )

        val broken = mutableListOf<String>()
        for ((key, english) in en) {
            val translated = pt[key] ?: continue
            if (placeholders(english) != placeholders(translated)) {
                broken.add("$key: values=${placeholders(english)}  pt-rBR=${placeholders(translated)}")
            }
        }
        assertEquals(
            "Placeholder mismatch inside a plural arm:\n  " + broken.joinToString("\n  "),
            emptyList<String>(),
            broken,
        )
    }

    @Test
    fun translatedStringsAreBrazilianPortugueseNotEnglishAndNotEuropean() {
        val en = readStrings(defaultFile())
        val pt = readStrings(ptBrFile())

        val untranslated = pt.filter { (name, value) ->
            name !in BRAND_STRINGS && value.isNotBlank() && value == en[name]
        }.keys
        assertEquals(
            "These pt-BR values are identical to the English source, which is what an " +
                "unfinished translation looks like. If the value really is a product name, " +
                "add it to BRAND_STRINGS here so the choice is written down: $untranslated",
            emptySet<String>(),
            untranslated,
        )

        val european = mutableListOf<String>()
        for ((name, value) in pt) {
            EUROPEAN_FORMS.find(value)?.let { european.add("$name: \"${it.value}\"") }
        }
        assertEquals(
            "European Portuguese in the pt-BR catalogue. Wavr's Portuguese is Brazilian: " +
                "\"tela\" not \"ecrã\", \"câmera\" not \"câmara\", \"você\" not the European " +
                "forms:\n  " + european.joinToString("\n  "),
            emptyList<String>(),
            european,
        )
    }

    private companion object {
        /** `%1$s`, `%2$d`, `%s` — index optional, conversion required. */
        val SPEC = Regex("%(?:(\\d+)\\\$)?([a-zA-Z])")

        /**
         * Product names. "Wavr" and "Core" stay in English in the web catalogue
         * (frontend/js/locale-pt.js) too; the phone must not disagree with the
         * dashboard about what the thing is called.
         */
        val BRAND_STRINGS = setOf("app_name", "core_channel_name")

        /**
         * Unambiguously European lexicon, and deliberately narrow. "divisão" is
         * the European word for a room but is also ordinary Brazilian Portuguese
         * for "division", so listing it here would eventually cry wolf.
         */
        val EUROPEAN_FORMS = Regex(
            "(?i)(?<![\\p{L}])(ecrã|ecran|câmara|câmaras|utilizador|utilizadores|" +
                "telemóvel|telemóveis|ficheiro|ficheiros|autocarro)(?![\\p{L}])"
        )
    }
}

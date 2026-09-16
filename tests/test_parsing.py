"""Parsing and matching tests. Run: python3 -m unittest discover -s tests -v"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cwa_organizer.passes.authors import AuthorNormalizer, split_author  # noqa: E402
from cwa_organizer.passes.classify import decide  # noqa: E402
from cwa_organizer.passes.common import junk_author_info, medium_from_probe, publisher_class  # noqa: E402
from cwa_organizer.passes.publishers import PublisherNormalizer, pub_key  # noqa: E402
from cwa_organizer.passes.series import skey  # noqa: E402
from cwa_organizer.passes.tags import TagNormalizer  # noqa: E402
from cwa_organizer.providers.calibre_fetch import parse_opf  # noqa: E402
from cwa_organizer.providers.mangaupdates import medium_of, mu_author_name  # noqa: E402
from cwa_organizer.textutil import clean_text, decode_entities, romaji_key  # noqa: E402
from cwa_organizer.titles import parse_title, standard_title, title_variants  # noqa: E402
from collections import Counter  # noqa: E402
import tomllib  # noqa: E402

DEFAULTS = tomllib.load(open(os.path.join(os.path.dirname(__file__), "..", "cwa_organizer", "defaults.toml"), "rb"))


def P(t, **kw):
    return parse_title(t, **kw)


class TitleParsing(unittest.TestCase):
    def check(self, title, series, volume, kind="explicit", part=None, subtitle=None, marker=None):
        p = P(title)
        self.assertEqual((p.series, p.volume, p.kind, p.part, p.subtitle, p.marker),
                         (series, volume, kind, part, subtitle, marker), title)

    def test_vol_forms(self):
        self.check("Death March to the Parallel World Rhapsody, Vol. 7", "Death March to the Parallel World Rhapsody", 7.0)
        self.check("Classroom of the Elite Vol.3", "Classroom of the Elite", 3.0)
        self.check("Cooking with Wild Game: Volume 33", "Cooking with Wild Game", 33.0)
        self.check("Seventh - Volume 03", "Seventh", 3.0)
        self.check("Vampire Hunter D Vol 01", "Vampire Hunter D", 1.0)
        self.check("My Youth Romantic Comedy Is Wrong, As I Expected, Vol. 10.5", "My Youth Romantic Comedy Is Wrong, As I Expected", 10.5)

    def test_tome_and_book_and_hash(self):
        self.check("L'Étranger Tome 03", "L'Étranger", 3.0)
        self.check("The Twelve Kingdoms Book 2", "The Twelve Kingdoms", 2.0)
        self.check("Harry Potter #3", "Harry Potter", 3.0)

    def test_old_bug_volume_dot(self):
        # Old sed rule turned "Vol. 3" into "Volume. 3"
        p = P("Spice & Wolf, Vol. 3")
        self.assertEqual(standard_title(p), "Spice & Wolf, Vol. 3")
        for v in title_variants(p, 10):
            self.assertNotIn("Volume.", v)

    def test_old_bug_vol_substring(self):
        # "Revolution 2" must not be treated as containing "Vol"; it is only a bare candidate
        p = P("Revolution 2")
        self.assertEqual(p.kind, "bare")
        self.assertIsNone(P("Evolution").kind)

    def test_bare_numbers_are_only_candidates(self):
        self.assertEqual(P("Fahrenheit 451").kind, "bare")
        self.assertIsNone(P("Another 2001").kind)       # 4 digits: never a volume
        self.assertIsNone(P("Villainess Level 99").kind)  # blocked word
        self.check("Sword Art Online 14: Alicization Uniting", "Sword Art Online", 14.0, kind="bare", subtitle="Alicization Uniting")

    def test_subtitles_and_parts(self):
        self.check("Otherside Picnic: Volume 9 - The Fourth Kinds' Summer Holiday", "Otherside Picnic", 9.0,
                   subtitle="The Fourth Kinds' Summer Holiday")
        self.check("Rebuild World: Volume 2 Part 1", "Rebuild World", 2.0, part=1)
        self.check("The Misfit of Demon King Academy: Volume 12 Act 2", "The Misfit of Demon King Academy", 12.0, part=2)
        self.assertEqual(P("Rebuild World: Volume 2 Part 1").index(), 2.1)
        self.assertIsNone(P("Rebuild World: Volume 2 Part 1").index("skip"))
        self.check("BAKEMONOGATARI, Part 1: Monster Tale", "BAKEMONOGATARI", 1.0, subtitle="Monster Tale")
        self.assertEqual(P("Ascendance of a Bookworm: Part 2 Apprentice Shrine Maiden Volume 2").kind, "complex")
        self.check("Another World Survival: Min-maxing My Support and Summoning Magic - Volume 06: Min-maxing My Support and Summoning Magic",
                   "Another World Survival: Min-maxing My Support and Summoning Magic", 6.0)

    def test_markers(self):
        self.check("Durarara!!, Vol. 3 (Novel)", "Durarara!!", 3.0, marker="novel")
        self.check("Sword Art Online Progressive 1 (light novel)", "Sword Art Online Progressive", 1.0, kind="bare", marker="novel")
        self.check("Tokyo Ghoul, Vol. 2 (Manga)", "Tokyo Ghoul", 2.0, marker="manga")
        self.check("Durarara!!, Vol. 9: (Durarara!! (novel))", "Durarara!!", 9.0, marker="novel")

    def test_junk_and_brackets(self):
        self.assertEqual(P("Overlord, Vol. 3 [Yen Press][Kobo][0EDF6669]").cleaned, "Overlord, Vol. 3")
        self.assertEqual(P("[Oshi no Ko], Vol. 3").series, "[Oshi no Ko]")
        self.assertEqual(standard_title(P("Ai no Kusabi[Vol8]")), "Ai no Kusabi, Vol. 8")
        self.assertEqual(P("Overlord, Vol. 1 (Yen Press)", publishers=["Yen Press"]).cleaned, "Overlord, Vol. 1")
        self.assertEqual(P("01 Beloved by the Male Lead's Nephew.azw").cleaned, "01 Beloved by the Male Lead's Nephew")
        self.assertEqual(P("Beastars Manga 16-Book Set, Vol. 1-16 by P").kind, "range")
        self.assertEqual(standard_title(P("My Favorite Song ~The Silver Siren~ Vol. 1")), "My Favorite Song ~The Silver Siren~, Vol. 1")
        self.assertEqual(standard_title(P("Re:ZERO -Starting Life in Another World-, Vol. 24")), "Re:ZERO -Starting Life in Another World-, Vol. 24")

    def test_entities_in_titles(self):
        self.assertEqual(P("Spice &amp; Wolf, Vol. 1").series, "Spice & Wolf")
        self.assertEqual(decode_entities("Comics &amp;amp; Graphic Novels"), "Comics & Graphic Novels")


class TagsAndOpf(unittest.TestCase):
    def setUp(self):
        self.n = TagNormalizer(DEFAULTS["tags"])

    def test_amp_and_synonyms(self):
        self.assertEqual(self.n.normalize(["Comics &amp; Graphic Novels", "light novel", "Sci-fi"]),
                         ["Comics & Graphic Novels", "Light Novel", "Science Fiction"])

    def test_case_duplicates_and_junk(self):
        self.assertEqual(self.n.normalize(["Fantasy", "fantasy", "ebook", "New Subject"]), ["Fantasy"])

    def test_bisac_and_loc(self):
        self.assertEqual(self.n.normalize(["Fiction / Fantasy / General"]), ["Fiction", "Fantasy"])
        self.assertEqual(self.n.normalize(["CYAC: Pirates--Fiction. | LCGFT: Light novels"]), ["Pirates", "Light Novel"])

    def test_author_name_tag_dropped(self):
        self.assertEqual(self.n.normalize(["Arata Kanoh", "Drama"], ["Arata Kanoh"]), ["Drama"])

    def test_opf_entities_decoded(self):
        opf = """<?xml version='1.0' encoding='utf-8'?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
<dc:title>Spice &amp; Wolf, Vol. 1 (light novel)</dc:title>
<dc:creator opf:role="aut">Isuna Hasekura</dc:creator><dc:creator opf:role="ill">Jyuu Ayakura</dc:creator>
<dc:publisher>Yen On &amp;amp; Friends</dc:publisher>
<dc:subject>Comics &amp; Graphic Novels</dc:subject><dc:language>eng</dc:language>
<dc:identifier opf:scheme="ISBN">9780759531048</dc:identifier><dc:identifier opf:scheme="calibre">x</dc:identifier>
<dc:date>2009-12-15T05:00:00+00:00</dc:date>
<meta name="calibre:series" content="Spice &amp; Wolf"/><meta name="calibre:series_index" content="1"/>
</metadata></package>"""
        m = parse_opf(opf)
        self.assertEqual(m.title, "Spice & Wolf, Vol. 1 (light novel)")
        self.assertEqual(m.authors, ["Isuna Hasekura"])
        self.assertEqual(m.publisher, "Yen On & Friends")
        self.assertEqual(m.tags, ["Comics & Graphic Novels"])
        self.assertEqual(m.identifiers, {"isbn": "9780759531048"})
        self.assertEqual((m.series, m.series_index), ("Spice & Wolf", 1.0))
        self.assertIn("Comics & Graphic Novels", self.n.normalize(m.tags))


class Classification(unittest.TestCase):
    pubs = DEFAULTS["publishers"]

    def d(self, **kw):
        base = dict(file_medium=None, title_marker=None, tag_medium=None, pub_class=None, mu_media=set(), mu_searched=True)
        base.update(kw)
        return decide(**base)[0]

    def test_publisher_classes(self):
        self.assertEqual(publisher_class("J-Novel Club", self.pubs), "ln")
        self.assertEqual(publisher_class("Kodansha Comics", self.pubs), "manga")
        self.assertEqual(publisher_class("Kodansha", self.pubs), "mixed")  # old bug: Kodansha manga got LN tag
        self.assertEqual(publisher_class("Vertical", self.pubs), "mixed")
        self.assertEqual(publisher_class("VIZ Media", self.pubs), "mixed")
        self.assertEqual(publisher_class("Orbit", self.pubs), "general")
        self.assertEqual(publisher_class("Editor's Choice Press", self.pubs), None)  # "tor" is a whole word only
        self.assertEqual(publisher_class("Haikasoru/VIZ Media", self.pubs), "ln")

    def test_kodansha_manga_not_light_novel(self):
        self.assertEqual(self.d(file_medium="comic", pub_class="mixed"), "manga")
        self.assertEqual(self.d(title_marker="manga", pub_class="mixed", tag_medium="novel"), "manga")

    def test_mu_lists_both_editions(self):
        self.assertEqual(self.d(file_medium="novel", mu_media={"novel", "comic"}), "novel")
        self.assertEqual(self.d(file_medium="comic", mu_media={"novel", "comic"}), "manga")
        self.assertIsNone(self.d(mu_media={"novel", "comic"}))  # PDF, no other evidence

    def test_other_books_needs_evidence(self):
        self.assertEqual(self.d(file_medium="novel", pub_class="general"), "other")
        self.assertIsNone(self.d(file_medium="novel", pub_class="general", mu_searched=False))
        self.assertIsNone(self.d(file_medium="novel", pub_class="mixed"))
        self.assertIsNone(self.d(file_medium="novel", tag_medium="novel", pub_class="general"))

    def test_conflicts(self):
        self.assertIsNone(self.d(file_medium="comic", title_marker="novel"))
        self.assertIsNone(self.d(file_medium="comic", pub_class="general"))

    def test_file_probe(self):
        self.assertEqual(medium_from_probe({"ok": True, "images": 180, "text_bytes": 90000, "html_files": 180}), "comic")
        self.assertEqual(medium_from_probe({"ok": True, "images": 12, "text_bytes": 450000, "html_files": 30}), "novel")
        self.assertIsNone(medium_from_probe({"ok": False, "images": 0, "text_bytes": 0}))

    def test_mu_helpers(self):
        self.assertEqual(medium_of("Novel"), "novel")
        self.assertEqual(medium_of("Manhwa"), "comic")
        self.assertIsNone(medium_of("Artbook"))
        self.assertEqual(mu_author_name("AINANA Hiro"), "Hiro Ainana")
        self.assertEqual(mu_author_name("Reki Kawahara"), "Reki Kawahara")


class AuthorsPublishersSeries(unittest.TestCase):
    def test_split(self):
        self.assertEqual(split_author("Fujino Omori, Kiyotaka Haimura")[0], ["Fujino Omori", "Kiyotaka Haimura"])
        self.assertEqual(split_author("Omori, Fujino")[0], ["Omori, Fujino"])
        self.assertEqual(split_author("Nisioisin, Illustrated by Vofan"), (["Nisioisin"], ["Vofan"]))
        self.assertEqual(split_author("Watsuki, Nobuhiro;Shizuka, Kaoru, 1958-")[0], ["Nobuhiro Watsuki", "Kaoru Shizuka"])
        self.assertEqual(split_author("Hirukuma,")[0], ["Hirukuma"])

    def test_normalizer(self):
        n = AuthorNormalizer(Counter({"Fujino Omori": 20, "Omori, Fujino": 3, "NISIOISIN": 5, "Nisio Isin": 9, "Lonely, Author": 1}), {})
        self.assertEqual(n.canonical("Omori, Fujino")[0], "Fujino Omori")
        self.assertEqual(n.canonical("NISIOISIN")[0], "Nisio Isin")
        self.assertEqual(n.canonical("Lonely, Author")[0], "Lonely, Author")  # no counterpart: suggestion only
        self.assertIn("Lonely, Author", n.flip_suggestions)
        self.assertEqual(romaji_key("Ao Juumonji"), romaji_key("Ao Jyumonji"))

    def test_junk_authors(self):
        j = junk_author_info("Vol 02 [June][Scans][4FF7E520]", [])
        self.assertEqual((j["volume"], j["publisher"]), (2.0, "June"))
        self.assertTrue(junk_author_info("Complete [VIZ][Scans Compressed][5E4B3DA6]", [])["complete"])
        self.assertIsNotNone(junk_author_info("VeryPDF", ["verypdf"]))
        self.assertIsNone(junk_author_info("Reki Kawahara", ["verypdf"]))

    def test_publishers(self):
        self.assertEqual(pub_key("VIZ Media, LLC"), pub_key("VIZMedia"))
        n = PublisherNormalizer(DEFAULTS["publishers"]["aliases"], {}, ["Square Enix", "Square Enix, Inc.", "Seven Seas", "Yen Press LLC"])
        self.assertEqual(n.canonical("Square Enix, Inc."), "Square Enix")
        self.assertEqual(n.canonical("Seven Seas"), "Seven Seas Entertainment")
        self.assertEqual(n.canonical("Yen Press, LLC"), "Yen Press")

    def test_series_key(self):
        self.assertEqual(skey("A Certain Magical Index"), skey("Certain Magical Index"))
        self.assertEqual(skey("Durarara!!"), skey("Durarara"))
        self.assertEqual(skey("The Devil Is a Part-Timer!"), skey("Devil is a Part-Timer"))
        self.assertNotEqual(skey("Classroom of the Elite"), skey("Classroom of the Elite: Year 2"))

    def test_clean_text(self):
        self.assertEqual(clean_text("  A　B  "), "A B")


if __name__ == "__main__":
    unittest.main()

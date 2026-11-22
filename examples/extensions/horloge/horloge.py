"""
Exemple d'EXTENSION (docs/extensions.md), aux deux moteurs : `horloge`,
exécuté par le proxy (l'heure dans un fuseau), et `local_time`, exécuté par
le navigateur (l'heure de l'appareil de l'utilisateur : horloge.js). À
copier, sous-dossier compris (horloge/horloge.py, horloge/horloge.js),
dans le dossier de [extensions].path, monté en lecture seule.

  [extensions.horloge]
  timezone = "Europe/Paris"   # le fuseau quand le modèle n'en donne pas
"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from llm_proxy import extension as ext

REQUIRES = 1


class Horloge(ext.Tool):
    name = "horloge"

    def prompt(self, present):
        # Le prompt de l'outil : la description de sa fonction, en anglais.
        return ("Give the current date and time in a time zone. Use it when the "
                "answer depends on the current date or time; never guess them.")

    def parameters(self, present):
        return {"type": "object", "properties": {
            "timezone": {"type": "string", "description": (
                "IANA time zone, e.g. Europe/Paris; omit for the default one.")},
        }}

    async def run(self, args, call):
        default = ext.settings("horloge").get("timezone") or "UTC"
        zone = str(args.get("timezone") or default)
        try:
            now = datetime.now(ZoneInfo(zone))
        except (ZoneInfoNotFoundError, ValueError):
            raise ext.ToolError("invalid_input", f"unknown time zone {zone!r}.")
        return ext.Result(f"{now:%Y-%m-%d %H:%M:%S} ({zone})",
                          meta={"timezone": zone})


# Un outil du NAVIGATEUR : sa déclaration ici, son exécution dans
# horloge.js (même nom). Sa famille (le menu Outils de la page) : celle de
# l'extension par défaut ; FAMILY la nomme et la décrit.
LOCAL_TIME = ext.BrowserTool(
    "local_time",
    description="Donne la date et l'heure de l'appareil de l'utilisateur, et son fuseau.",
)

FAMILY = ext.Family("horloge", "Horloge", tools=("local_time",),
                    hint="l'heure de l'appareil", on=True)

TOOLS = [Horloge(), LOCAL_TIME]

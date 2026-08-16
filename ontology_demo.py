"""
Proof that the storage layer doesn't need to know which ontology it's
speaking: load Brick's real published ontology as data, add one building's
instance data, query it with real SPARQL. Swap in a different ontology file
and the same loader/query code works unchanged - that's the "pick at load
time" idea made concrete.
"""

from rdflib import Graph, Namespace, RDF

BRICK = Namespace("https://brickschema.org/schema/Brick#")
BLDG = Namespace("urn:mybuilding#")

g = Graph()
g.parse("/tmp/Brick.ttl", format="turtle")
print(f"loaded Brick ontology: {len(g)} triples")

# instance data: our building's actual equipment, using Brick's vocabulary
g.add((BLDG.chiller_1, RDF.type, BRICK.Chiller))
g.add((BLDG.ahu_1, RDF.type, BRICK.AHU))
g.add((BLDG.chiller_1, BRICK.feeds, BLDG.ahu_1))

result = g.query("""
    PREFIX brick: <https://brickschema.org/schema/Brick#>
    PREFIX bldg: <urn:mybuilding#>
    SELECT ?fed WHERE { bldg:chiller_1 brick:feeds ?fed }
""")

for row in result:
    print(f"chiller_1 feeds -> {row.fed}")

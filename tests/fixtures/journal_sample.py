# NX 2406 Python journal (synthetic fixture for CAD-AI-Monitor unit tests)
# Journal created by: synthetic fixture (2026-10-27, v0.2.3), not a real recording
#
#   Menu: File->New...
#   Menu: Insert->Sketch...
#   Menu: Insert->Design Feature->Extrude...
session = NXOpen.Session.GetSession()
workPart = session.Parts.Work
featureCollection = workPart.Features
NXOpen.Features.FeatureCollection.CreateExtrude(null)
# EOF

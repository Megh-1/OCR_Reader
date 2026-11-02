"""
The vision-model reader for air waybills: crops (crops.py), the questions put to the model (waybill.py), a client of the reader
service (client.py), what to make of its answers (decide.py), an independent second reader to compare it with (words.py) and the
page-level entry point (reader.py). The experiment that produced these rules is in scripts/ (score_vlm.py and friends); it imports
this package so the measured rules and the shipped rules are one piece of code.
"""

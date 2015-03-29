#!/bin/bash

sudo python setup.py install

gemini query -q "select variant_id, chrom, sub_type from variants" \
		--gt-filter "[gt_depth].[sex='1'].[>100].[all] && gt_type.child_1 = HET" \
		--header --show-samples

gemini query -q "select variant_id, (gt_types).(sex='1') from variants" \
		--header

gemini query -q "select * from samples where sex = '1'" --header

gemini query -q "select variant_id from variants" --gt-filter "[gt_depth].[*].[<240].[any]" --header

gemini query -q "select variant_id from variants" --gt-filter "[gt_depth].[*].[<240].[none]" --cores 2

gemini query -q "select variant_id from variants" --gt-filter "[gt_depth].[*].[<100].[count < 3]" --cores 1

gemini query -q "select variant_id from variants" --gt-filter "[gt_type].[*].[=HET].[count <= 2]"

gemini query -q "select variant_id from variants" --gt-filter "[gt_type].[*].[!=HET].[any]" --cores 2

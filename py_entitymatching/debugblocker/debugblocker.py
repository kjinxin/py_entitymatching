from collections import namedtuple
import heapq as hq
import logging
import numpy
from operator import attrgetter
import pandas as pd
from py_entitymatching.utils.validation_helper import validate_object_type

import py_entitymatching as em
import py_entitymatching.catalog.catalog_manager as cm

logger = logging.getLogger(__name__)


def debug_blocker(candset, ltable, rtable, output_size=200,
                  attr_corres=None, verbose=False):
    """
    This function debugs the blocker output and reports a list of potential
    matches that are discarded by a blocker (or a blocker sequence).

    Specifically,  this function takes in the two input tables for
    matching and the candidate set returned by a blocker (or a blocker
    sequence), and produces a list of tuple pairs which are rejected by the
    blocker but with high potential of being true matches.

    Args:
        candset (DataFrame): The candidate set generated by
            applying the blocker on the ltable and rtable.
        ltable,rtable (DataFrame): The input DataFrames that are used to
            generate the blocker output.
        output_size (int): The number of tuple pairs that will be
            returned (defaults to 200).
        attr_corres (list): A list of attribute correspondence tuples.
            When ltable and rtable have different schemas, or the same
            schema but different words describing the attributes, the
            user needs to manually specify the attribute correspondence.
            Each element in this list should be a tuple of strings
            which are the corresponding attributes in ltable and rtable.
            The default value is None, and if the user doesn't specify
            this list, a built-in function for finding the
            attribute correspondence list will be called. But we highly
            recommend the users manually specify the attribute
            correspondences, unless the schemas of ltable and rtable are
            identical (defaults to None).
        verbose (boolean):  A flag to indicate whether the debug information
         should be logged (defaults to False).

    Returns:
        A pandas DataFrame with 'output_size' number of rows. Each row in the
        DataFrame is a tuple pair which has potential of being a true
        match, but is rejected by the blocker (meaning that the tuple
        pair is in the Cartesian product of ltable and rtable subtracted
        by the candidate set). The fields in the returned DataFrame are
        from ltable and rtable, which are useful for determining similar
        tuple pairs.

    Raises:
        AssertionError: If `ltable`, `rtable` or `candset` is not of type
            pandas DataFrame.
        AssertionError: If `ltable` or `rtable` is empty (size of 0).
        AssertionError: If the output `size` parameter is less than or equal
            to 0.
        AssertionError: If the attribute correspondence (`attr_corres`) list is
            not in the correct format (a list of tuples).
        AssertionError: If the attribute correspondence (`attr_corres`)
            cannot be built correctly.

    Examples:
        >>> import py_entitymatching as em
        >>> ob = em.OverlapBlocker()
        >>> C = ob.block_tables(A, B, l_overlap_attr='title', r_overlap_attr='title', overlap_size=3)
        >>> corres = [('ID','ssn'), ('name', 'ename'), ('address', 'location'),('zipcode', 'zipcode')]
        >>> D = em.debug_blocker(C, A, B, attr_corres=corres)

        >>> import py_entitymatching as em
        >>> ob = em.OverlapBlocker()
        >>> C = ob.block_tables(A, B, l_overlap_attr='name', r_overlap_attr='name', overlap_size=3)
        >>> D = em.debug_blocker(C, A, B, output_size=150)

    """
    # Check input types.
    _validate_types(ltable, rtable, candset, output_size,
                    attr_corres, verbose)

    # Check table size.
    if len(ltable) == 0:
        raise AssertionError('Error: ltable is empty!')
    if len(rtable) == 0:
        raise AssertionError('Error: rtable is empty!')

    # Check the value of output size.
    if output_size <= 0:
        raise AssertionError('The input parameter: \'output_size\''
                             ' is less than or equal to 0. Nothing needs'
                             ' to be done!')

    # Get table metadata.
    l_key, r_key = cm.get_keys_for_ltable_rtable(ltable, rtable, logger, verbose)

    # Validate metadata
    cm._validate_metadata_for_table(ltable, l_key, 'ltable', logger, verbose)
    cm._validate_metadata_for_table(rtable, r_key, 'rtable', logger, verbose)

    # Check the user input field correst list (if exists) and get the raw
    # version of our internal correst list.
    _check_input_field_correspondence_list(ltable, rtable, attr_corres)
    corres_list = _get_field_correspondence_list(ltable, rtable,
                                                 l_key, r_key, attr_corres)

    # Build the (col_name: col_index) dict to speed up locating a field in
    # the schema.
    ltable_col_dict = _build_col_name_index_dict(ltable)
    rtable_col_dict = _build_col_name_index_dict(rtable)

    # Filter correspondence list to remove numeric types. We only consider
    # string types for document concatenation.
    _filter_corres_list(ltable, rtable, l_key, r_key,
                        ltable_col_dict, rtable_col_dict, corres_list)

    # Get field filtered new table.
    ltable_filtered, rtable_filtered = _get_filtered_table(
        ltable, rtable, l_key, r_key, corres_list)

    # Select a subset of fields with high scores.
    feature_list = _select_features(ltable_filtered, rtable_filtered, l_key)

    # Map the record key value to its index in the table.
    lrecord_id_to_index_map = _get_record_id_to_index_map(ltable_filtered, l_key)
    rrecord_id_to_index_map = _get_record_id_to_index_map(rtable_filtered, r_key)

    # Build the tokenized record list delimited by a white space on the
    # selected fields.
    lrecord_list = _get_tokenized_table(ltable_filtered, l_key, feature_list)
    rrecord_list = _get_tokenized_table(rtable_filtered, r_key, feature_list)

    # Reformat the candidate set from a dataframe to a list of record index
    # tuple pair.
    new_formatted_candidate_set = _index_candidate_set(
        candset, lrecord_id_to_index_map, rrecord_id_to_index_map, verbose)

    # Build the token order according to token's frequency. To run a
    # prefix filtering based similarity join algorithm, we first need
    # the global token order.
    order_dict = {}
    _build_global_token_order(lrecord_list, order_dict)
    _build_global_token_order(rrecord_list, order_dict)

    # Sort the tokens in each record by the global order.
    _sort_record_tokens_by_global_order(lrecord_list, order_dict)
    _sort_record_tokens_by_global_order(rrecord_list, order_dict)

    # Run the topk similarity join.
    topk_heap = _topk_sim_join(
        lrecord_list, rrecord_list, new_formatted_candidate_set, output_size)

    # Assemble the topk record list to a dataframe.
    ret_dataframe = _assemble_topk_table(topk_heap, ltable_filtered, rtable_filtered)
    return ret_dataframe


# Validate the types of input parameters.
def _validate_types(ltable, rtable, candidate_set, output_size,
                    attr_corres, verbose):
    validate_object_type(ltable, pd.DataFrame, 'Input left table')

    validate_object_type(rtable, pd.DataFrame, 'Input right table')

    validate_object_type(candidate_set, pd.DataFrame, 'Input candidate set')

    validate_object_type(output_size, int, 'Output size')

    if attr_corres is not None:
        if not isinstance(attr_corres, list):
            logging.error('Input attribute correspondence is not of'
                          ' type list')
            raise AssertionError('Input attribute correspondence is'
                                 ' not of type list')

        for pair in attr_corres:
            if not isinstance(pair, tuple):
                logging.error('Pair in attribute correspondence list is not'
                              ' of type tuple')
                raise AssertionError('Pair in attribute correspondence list'
                                     ' is not of type tuple')

    if not isinstance(verbose, bool):
        logger.error('Parameter verbose is not of type bool')
        raise AssertionError('Parameter verbose is not of type bool')


# Assemble the topk heap to a dataframe.
def _assemble_topk_table(topk_heap, ltable, rtable, ret_key='_id',
                         l_output_prefix='ltable_', r_output_prefix='rtable_'):
    topk_heap.sort(key=lambda tup: tup[0], reverse=True)
    ret_data_col_name_list = ['_id', 'similarity']
    ltable_col_names = list(ltable.columns)
    rtable_col_names = list(rtable.columns)
    lkey = em.get_key(ltable)
    rkey = em.get_key(rtable)
    lkey_index = 0
    rkey_index = 0
    for i in range(len(ltable_col_names)):
        if ltable_col_names[i] == lkey:
            lkey_index = i

    for i in range(len(rtable_col_names)):
        if rtable_col_names[i] == rkey:
            rkey_index = i

    ret_data_col_name_list.append(l_output_prefix + lkey)
    ret_data_col_name_list.append(r_output_prefix + rkey)
    ltable_col_names.remove(lkey)
    rtable_col_names.remove(rkey)

    for i in range(len(ltable_col_names)):
        ret_data_col_name_list.append(l_output_prefix + ltable_col_names[i])
    for i in range(len(rtable_col_names)):
        ret_data_col_name_list.append(r_output_prefix + rtable_col_names[i])

    ret_tuple_list = []
    for i in range(len(topk_heap)):
        tup = topk_heap[i]
        lrecord = list(ltable.ix[tup[1]])
        rrecord = list(rtable.ix[tup[2]])
        ret_tuple = [i, tup[0]]
        ret_tuple.append(lrecord[lkey_index])
        ret_tuple.append(rrecord[rkey_index])
        for j in range(len(lrecord)):
            if j != lkey_index:
                ret_tuple.append(lrecord[j])
        for j in range(len(rrecord)):
            if j != rkey_index:
                ret_tuple.append(rrecord[j])
        ret_tuple_list.append(ret_tuple)

    data_frame = pd.DataFrame(ret_tuple_list)
    # When the ret data frame is empty, we cannot assign column names.
    if len(data_frame) == 0:
        return data_frame

    data_frame.columns = ret_data_col_name_list
    lkey = em.get_key(ltable)
    rkey = em.get_key(rtable)
    cm.set_candset_properties(data_frame, ret_key, l_output_prefix + lkey,
                              r_output_prefix + rkey, ltable, rtable)

    return data_frame


# Topk similarity join wrapper.
def _topk_sim_join(lrecord_list, rrecord_list, cand_set, output_size):
    # Build prefix events.
    prefix_events = _generate_prefix_events(lrecord_list, rrecord_list)
    topk_heap = _topk_sim_join_impl(lrecord_list, rrecord_list,
                                    prefix_events, cand_set, output_size)

    return topk_heap


# Implement topk similarity join. Refer to "top-k set similarity join"
# by Xiao et al. for details.
def _topk_sim_join_impl(lrecord_list, rrecord_list, prefix_events,
                        cand_set, output_size):
    total_compared_pairs = 0
    compared_set = set()
    l_inverted_index = {}
    r_inverted_index = {}
    topk_heap = []

    while len(prefix_events) > 0:
        if len(topk_heap) == output_size and\
                        topk_heap[0][0] >= prefix_events[0][0] * -1:
            break
        event = hq.heappop(prefix_events)
        table_indicator = event[1]
        rec_idx = event[2]
        tok_idx = event[3]
        if table_indicator == 0:
            token = lrecord_list[rec_idx][tok_idx]
            if token in r_inverted_index:
                r_records = r_inverted_index[token]
                for r_rec_idx in r_records:
                    pair = (rec_idx, r_rec_idx)
                    # Skip if the pair is in the candidate set.
                    if pair in cand_set:
                        continue
                    # Skip if the pair has been compared.
                    if pair in compared_set:
                        continue
                    sim = _jaccard_sim(
                        set(lrecord_list[rec_idx]), set(rrecord_list[r_rec_idx]))
                    if len(topk_heap) == output_size:
                        hq.heappushpop(topk_heap, (sim, rec_idx, r_rec_idx))
                    else:
                        hq.heappush(topk_heap, (sim, rec_idx, r_rec_idx))

                    total_compared_pairs += 1
                    compared_set.add(pair)

            # Update the inverted index.
            if token not in l_inverted_index:
                l_inverted_index[token] = set()
            l_inverted_index[token].add(rec_idx)
        else:
            token = rrecord_list[rec_idx][tok_idx]
            if token in l_inverted_index:
                l_records = l_inverted_index[token]
                for l_rec_idx in l_records:
                    pair = (l_rec_idx, rec_idx)
                    # Skip if the pair is in the candidate set.
                    if pair in cand_set:
                        continue
                    # Skip if the pair has been compared.
                    if pair in compared_set:
                        continue
                    sim = _jaccard_sim(
                        set(lrecord_list[l_rec_idx]), set(rrecord_list[rec_idx]))
                    if len(topk_heap) == output_size:
                        hq.heappushpop(topk_heap, (sim, l_rec_idx, rec_idx))
                    else:
                        hq.heappush(topk_heap, (sim, l_rec_idx, rec_idx))

                    total_compared_pairs += 1
                    compared_set.add(pair)

            # Update the inverted index.
            if token not in r_inverted_index:
                r_inverted_index[token] = set()
            r_inverted_index[token].add(rec_idx)

    return topk_heap


# Calculate the token-based Jaccard similarity of two string sets.
def _jaccard_sim(l_token_set, r_token_set):
    l_len = len(l_token_set)
    r_len = len(r_token_set)
    intersect_size = len(l_token_set & r_token_set)
    if l_len + r_len == 0:
        return 0.0

    return intersect_size * 1.0 / (l_len + r_len - intersect_size)


# Check the input field correspondence list.
def _check_input_field_correspondence_list(ltable, rtable, field_corres_list):
    if field_corres_list is None:
        return
    true_ltable_fields = list(ltable.columns)
    true_rtable_fields = list(rtable.columns)
    for pair in field_corres_list:
        # Raise an error if the pair in not a tuple or the length is not two.
        if type(pair) != tuple or len(pair) != 2:
            raise AssertionError('Error in checking user input field'
                                 ' correspondence: the input field pairs'
                                 'are not in the required tuple format!')

    given_ltable_fields = [field[0] for field in field_corres_list]
    given_rtable_fields = [field[1] for field in field_corres_list]

    # Raise an error if a field is in the correspondence list but not in
    # the table schema.
    for given_field in given_ltable_fields:
        if given_field not in true_ltable_fields:
            raise AssertionError('Error in checking user input field'
                                 ' correspondence: the field \'%s\' is'
                                 ' not in the ltable!' % given_field)
    for given_field in given_rtable_fields:
        if given_field not in true_rtable_fields:
            raise AssertionError('Error in checking user input field'
                                 ' correspondence:'
                                 ' the field \'%s\' is not in the'
                                 ' rtable!' % given_field)
    return


# Get the field correspondence list. If the input list is empty, call
# the system builtin function to get the correspondence, or use the
# user input as the correspondence.
def _get_field_correspondence_list(ltable, rtable, lkey, rkey, attr_corres):
    corres_list = []
    if attr_corres is None or len(attr_corres) == 0:
        corres_list = em.get_attr_corres(ltable, rtable)['corres']
        if len(corres_list) == 0:
            raise AssertionError('Error: the field correspondence list'
                                 ' is empty. Please specify the field'
                                 ' correspondence!')
    else:
        for tu in attr_corres:
            corres_list.append(tu)

    # If the key correspondence is not in the list, add it in.
    key_pair = (lkey, rkey)
    if key_pair not in corres_list:
        corres_list.append(key_pair)

    return corres_list


# Filter the correspondence list. Remove the fields in numeric types.
def _filter_corres_list(ltable, rtable, ltable_key, rtable_key,
                        ltable_col_dict, rtable_col_dict, corres_list):
    ltable_dtypes = list(ltable.dtypes)
    rtable_dtypes = list(rtable.dtypes)
    for i in reversed(range(len(corres_list))):
        lcol_name = corres_list[i][0]
        rcol_name = corres_list[i][1]
        # Filter the pair where both fields are numeric types.
        if ltable_dtypes[ltable_col_dict[lcol_name]] != numpy.dtype('O')\
                and rtable_dtypes[rtable_col_dict[rcol_name]] != numpy.dtype('O'):
            if lcol_name != ltable_key and rcol_name != rtable_key:
                corres_list.pop(i)

    if len(corres_list) == 1 and corres_list[0][0] == ltable_key\
                             and corres_list[0][1] == rtable_key:
        raise AssertionError('The field correspondence list is empty after'
                             ' filtering: please verify your correspondence'
                             ' list, or check if each field is of numeric'
                             ' type!')


# Filter the original input tables according to the correspondence list.
# The filtered tables will only contain the fields in the correspondence list.
def _get_filtered_table(ltable, rtable, lkey, rkey, corres_list):
    ltable_cols = [col_pair[0] for col_pair in corres_list]
    rtable_cols = [col_pair[1] for col_pair in corres_list]
    lfiltered_table = ltable[ltable_cols]
    rfiltered_table = rtable[rtable_cols]
    em.set_key(lfiltered_table, lkey)
    em.set_key(rfiltered_table, rkey)
    return lfiltered_table, rfiltered_table


# Build the mapping bewteen field name and its index in the schema.
def _build_col_name_index_dict(table):
    col_dict = {}
    col_names = list(table.columns)
    for i in range(len(col_names)):
        col_dict[col_names[i]] = i
    return col_dict


# Select the most important fields for similarity join. The importance
# of a fields is measured by the combination of field value uniqueness
# and non-emptyness.
def _select_features(ltable, rtable, lkey):
    lcolumns = list(ltable.columns)
    rcolumns = list(rtable.columns)
    lkey_index = -1
    if len(lcolumns) != len(rcolumns):
        raise AssertionError('Error: FILTERED ltable and FILTERED rtable'
                             ' have different number of fields!')
    for i in range(len(lcolumns)):
        if lkey == lcolumns[i]:
            lkey_index = i

    lweight = _get_feature_weight(ltable)
    rweight = _get_feature_weight(rtable)

    Rank = namedtuple('Rank', ['index', 'weight'])
    rank_list = []
    for i in range(len(lweight)):
        rank_list.append(Rank(i, lweight[i] * rweight[i]))
    rank_list.pop(lkey_index)

    rank_list = sorted(rank_list, key=attrgetter('weight'), reverse=True)
    rank_index_list = []
    num_selected_fields = 0
    if len(rank_list) <= 3:
        num_selected_fields = len(rank_list)
    elif len(rank_list) <= 5:
        num_selected_fields = 3
    else:
        num_selected_fields = int(len(rank_list) / 2)
    for i in range(num_selected_fields):
        rank_index_list.append(rank_list[i].index)
    return sorted(rank_index_list)


# Calculate the importance (weight) for each field in a table.
def _get_feature_weight(table):
    num_records = len(table)
    if num_records == 0:
        raise AssertionError('Error: empty table!')
    weight = []
    for col in table.columns:
        value_set = set()
        non_empty_count = 0
        col_values = table[col]
        for value in col_values:
            if not pd.isnull(value) and value != '':
                value_set.add(value)
                non_empty_count += 1
        selectivity = 0.0
        if non_empty_count != 0:
            selectivity = len(value_set) * 1.0 / non_empty_count
        non_empty_ratio = non_empty_count * 1.0 / num_records

        # The field weight is the combination of non-emptyness
        # and uniqueness.
        weight.append(non_empty_ratio + selectivity)
    return weight


# Build the mapping of record key value and its index in the table.
def _get_record_id_to_index_map(table, table_key):
    record_id_to_index = {}
    id_col = list(table[table_key])
    for i in range(len(id_col)):
        if id_col[i] in record_id_to_index:
            raise AssertionError('Duplicate keys found:', id_col[i])
        record_id_to_index[id_col[i]] = i
    return record_id_to_index


# Tokenize a table. First tokenize each table column by a white space,
# then concatenate the column of each record. The reason for tokenizing
# columns first is that it's more efficient than iterate each dataframe
# tuple.
def _get_tokenized_table(table, table_key, feature_list):
    record_list = []
    columns = table.columns[feature_list]
    tmp_table = []
    for col in columns:
        column_token_list = _get_tokenized_column(table[col])
        tmp_table.append(column_token_list)

    num_records = len(table[table_key])
    for i in range(num_records):
        token_list = []
        index_map = {}

        for j in range(len(columns)):
            tmp_col_tokens = tmp_table[j][i]
            for token in tmp_col_tokens:
                if token != '':
                    if token in index_map:
                        token_list.append(token + '_' + str(index_map[token]))
                        index_map[token] += 1
                    else:
                        token_list.append(token)
                        index_map[token] = 1
        record_list.append(token_list)

    return record_list


# Tokenize each table column by white spaces.
def _get_tokenized_column(column):
    column_token_list = []
    for value in list(column):
        tmp_value = _replace_nan_to_empty(value)
        if tmp_value != '':
            tmp_list = list(tmp_value.lower().split(' '))
            column_token_list.append(tmp_list)
        else:
            column_token_list.append([''])
    return column_token_list


# Check the value of each field. Replace nan with empty string.
# Cast floats into integers.
def _replace_nan_to_empty(field):
    if pd.isnull(field):
        return ''
    elif type(field) in [float, numpy.float64, int, numpy.int64]:
        return str('{0:.0f}'.format(field))
    else:
        return field


# Reformat the input candidate set. Since the input format is DataFrame,
# it's difficult for us to know if a tuple pair is in the candidate
# set or not. We will use the reformatted candidate set in the topk
# similarity join.
def _index_candidate_set(candidate_set, lrecord_id_to_index_map, rrecord_id_to_index_map, verbose):
    new_formatted_candidate_set = set()
    if len(candidate_set) == 0:
        return new_formatted_candidate_set

    # Get metadata
    key, fk_ltable, fk_rtable, ltable, rtable, l_key, r_key =\
        cm.get_metadata_for_candset(candidate_set, logger, verbose)

    # validate metadata
    cm._validate_metadata_for_candset(candidate_set, key, fk_ltable, fk_rtable,
                                     ltable, rtable, l_key, r_key,
                                     logger, verbose)

    ltable_key_data = list(candidate_set[fk_ltable])
    rtable_key_data = list(candidate_set[fk_rtable])

    for i in range(len(ltable_key_data)):
        new_formatted_candidate_set.add((lrecord_id_to_index_map[ltable_key_data[i]],
                                         rrecord_id_to_index_map[rtable_key_data[i]]))

    return new_formatted_candidate_set


# Build the global order of tokens in the table by frequency.
def _build_global_token_order(record_list, order_dict):
    for record in record_list:
        for token in record:
            if token in order_dict:
                order_dict[token] += 1
            else:
                order_dict[token] = 1


# Sort each tokenized record by the global token order.
def _sort_record_tokens_by_global_order(record_list, order_dict):
    for i in range(len(record_list)):
        tmp_record = []
        for token in record_list[i]:
            if token in order_dict:
                tmp_record.append(token)
        record_list[i] = sorted(tmp_record, key=lambda x: (order_dict[x], x))


# Generate the prefix events of two tables for topk similarity joins.
# Refer to "top-k set similarity join" by Xiao et al. for details.
def _generate_prefix_events(lrecord_list, rrecord_list):
    prefix_events = []
    _generate_prefix_events_impl(lrecord_list, prefix_events, 0)
    _generate_prefix_events_impl(rrecord_list, prefix_events, 1)

    return prefix_events


# Prefix event generation for a table.
def _generate_prefix_events_impl(record_list, prefix_events, table_indicator):
    for i in range(len(record_list)):
        length = len(record_list[i])
        for j in range(length):
            threshold = _calc_threshold(j, length)
            hq.heappush(prefix_events,
                        (-1.0 * threshold, table_indicator, i, j, record_list[i][j]))


# Calculate the corresponding topk similarity join of a token in a record.
# Refer to "top-k set similarity join" by Xiao et al. for details.
def _calc_threshold(token_index, record_length):
    return 1 - token_index * 1.0 / record_length
